from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlmodel import create_engine, Session, SQLModel
from .config import DATABASE_URL, DB_ECHO

# SQLite 사용 시 connect_args={"check_same_thread": False}가 필요함
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, echo=DB_ECHO)

# 스키마 변경 시 여기에 (테이블명, 컬럼명, ALTER 문) 을 추가한다.
# SQLModel.metadata.create_all()은 없는 테이블은 만들지만 기존 테이블에 컬럼을
# 추가하지는 않는다 (SQLAlchemy의 문서화된 동작). 이미 배포된 SQLite 파일은
# create_all만으로는 새 컬럼을 얻지 못하므로, 부팅 시 직접 보강한다.
_PENDING_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "agentreport",
        "provider_supports_tools",
        # DEFAULT 0: 이 컬럼이 없던 시절에 만들어진 기존 행을 "도구 미지원"으로
        # 채운다. NULL이나 1로 채우면 지어낸 과거 리포트가 검증된 것처럼 보일 수
        # 있으므로(#162) 반드시 0(False)이어야 한다.
        "ALTER TABLE agentreport ADD COLUMN provider_supports_tools BOOLEAN NOT NULL DEFAULT 0",
    ),
)


def _run_schema_migrations() -> None:
    """create_all이 다루지 못하는, 기존 테이블에 대한 컬럼 추가를 처리한다.

    check(컬럼 존재 여부 조회)와 act(ALTER TABLE)가 원자적이지 않으므로, 워커가
    둘 이상이면(현재는 Dockerfile이 --workers 없이 단일 프로세스로 띄우므로
    해당 없음, 하지만 그 가정이 바뀌는 순간 바로 재현된다) 동시에 여러 프로세스가
    "컬럼 없음"을 보고 똑같이 ALTER를 시도할 수 있다. 이때 늦게 도착한 쪽은
    SQLite로부터 "duplicate column name" OperationalError를 받는데, 이는 컬럼이
    이미 (다른 워커에 의해) 정상적으로 추가됐다는 뜻이므로 startup을 중단시킬
    이유가 아니다 — 그래서 이 경우만 흡수하고, 그 외의 OperationalError(예:
    디스크 권한 문제로 인한 진짜 실패)는 그대로 올려 startup이 조용히 절반만
    마이그레이션된 스키마로 계속되지 않게 한다.

    주의: BOOLEAN NOT NULL DEFAULT 0 구문과 "duplicate column name" 문자열 매칭은
    SQLite 전용이다. 다른 방언이면 조용히 오동작할 수 있으므로 가드를 둔다.
    """
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            f"스키마 마이그레이션은 SQLite 전용입니다 (현재: {engine.dialect.name}). "
            "다른 DB로 옮기려면 alembic을 도입하세요."
        )
    for table_name, column_name, alter_sql in _PENDING_COLUMN_MIGRATIONS:
        inspector = inspect(engine)
        if table_name not in inspector.get_table_names():
            # create_all이 이미 최신 스키마로 새로 만들었으므로 손댈 필요 없음
            continue
        columns = {col["name"] for col in inspector.get_columns(table_name)}
        if column_name in columns:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(alter_sql))
        except OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            # 다른 워커가 먼저 컬럼을 추가했다는 뜻인지 재확인한다 — 메시지가
            # 우연히 겹쳤을 뿐 실제로는 컬럼이 없는 상태라면 원래 예외를 올린다.
            refreshed_columns = {
                col["name"] for col in inspect(engine).get_columns(table_name)
            }
            if column_name not in refreshed_columns:
                raise


# A (#162): decision/confidence_score를 nullable로 변경하는 테이블 재생성.
# SQLite는 ALTER TABLE로 NOT NULL constraint 제거가 불가능하므로 (공식 권장:
# CREATE new → INSERT SELECT → DROP old → RENAME), 부팅 시 필요한 경우에만 실행.
# _run_schema_migrations()가 provider_supports_tools 컬럼을 먼저 추가하므로,
# 이 함수가 실행될 때 해당 컬럼은 항상 존재한다.
_RECREATE_AGENTREPORT_NULLABLE_DDL = (
    "CREATE TABLE agentreport_new ("
    "id INTEGER PRIMARY KEY, "
    "stock_code VARCHAR NOT NULL, "
    "stock_name VARCHAR NOT NULL, "
    "provider VARCHAR NOT NULL, "
    "summary VARCHAR NOT NULL, "
    "decision VARCHAR, "            # nullable: 도구 없는 provider는 null
    "confidence_score FLOAT, "      # nullable: 도구 없는 provider는 null
    "reason VARCHAR NOT NULL DEFAULT '', "
    "provider_supports_tools BOOLEAN NOT NULL DEFAULT 0, "
    "created_at DATETIME NOT NULL"
    ")"
)
_RECREATE_AGENTREPORT_COLS = (
    "id, stock_code, stock_name, provider, summary, decision, "
    "confidence_score, reason, provider_supports_tools, created_at"
)
# DROP TABLE은 그 테이블에 딸린 인덱스도 함께 지운다. 재생성 후 다시 만들지 않으면
# models.py의 index=True로 선언된 stock_code·stock_name 인덱스가 영구히 사라진다 —
# 테이블이 이미 존재하므로 create_all()은 다시 만들어 주지 않는다. 이름은 SQLModel이
# 붙이는 규칙(ix_<table>_<column>)을 그대로 따라야 create_all()과 충돌하지 않는다.
_RECREATE_AGENTREPORT_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_agentreport_stock_code ON agentreport (stock_code)",
    "CREATE INDEX IF NOT EXISTS ix_agentreport_stock_name ON agentreport (stock_name)",
)


def _run_table_recreate_migrations() -> None:
    """decision/confidence_score 컬럼을 nullable로 변경하는 agentreport 테이블 재생성.

    기존 행은 모두 NOT NULL 시절 저장돼 있으므로 decision/confidence_score 값을
    그대로 유지한다(NULL이 아닌 기존 값은 보존). 새로 저장하는 도구 없는 provider
    리포트만 NULL로 기록된다.

    주의: BOOLEAN NOT NULL DEFAULT 0, VARCHAR 타입명, "duplicate column" 등의 문자열은
    SQLite 전용이다. 다른 방언으로 전환할 때는 alembic을 도입해야 한다.
    """
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            f"테이블 재생성 마이그레이션은 SQLite 전용입니다 (현재: {engine.dialect.name}). "
            "다른 DB로 옮기려면 alembic을 도입하세요."
        )
    inspector = inspect(engine)
    if "agentreport" not in inspector.get_table_names():
        # create_all이 최신 스키마(nullable decision/confidence_score)로 새로 만들었으므로
        # 건드릴 필요 없음
        return

    col_info = {col["name"]: col for col in inspector.get_columns("agentreport")}
    decision_col = col_info.get("decision")
    if decision_col is None or decision_col.get("nullable", True):
        # 컬럼이 없거나(create_all이 최신 스키마로 만든 경우) 이미 nullable이면 skip
        return

    # decision이 NOT NULL인 경우: 테이블 재생성으로 nullable로 변경
    try:
        with engine.begin() as conn:
            conn.execute(text(_RECREATE_AGENTREPORT_NULLABLE_DDL))
            conn.execute(text(
                f"INSERT INTO agentreport_new ({_RECREATE_AGENTREPORT_COLS}) "
                f"SELECT {_RECREATE_AGENTREPORT_COLS} FROM agentreport"
            ))
            conn.execute(text("DROP TABLE agentreport"))
            conn.execute(text("ALTER TABLE agentreport_new RENAME TO agentreport"))
            for index_sql in _RECREATE_AGENTREPORT_INDEX_DDL:
                conn.execute(text(index_sql))
    except OperationalError as exc:
        # 동시 실행된 워커가 이미 재생성을 완료했는지 재확인한다.
        refreshed = {col["name"]: col for col in inspect(engine).get_columns("agentreport")}
        refreshed_col = refreshed.get("decision")
        if refreshed_col is not None and refreshed_col.get("nullable", True):
            return  # 다른 워커가 이미 완료한 상태
        raise


def init_db():
    """
    데이터베이스 테이블을 초기화합니다.
    """
    try:
        SQLModel.metadata.create_all(engine)
    except OperationalError as exc:
        if "already exists" not in str(exc).lower():
            raise
    _run_schema_migrations()
    _run_table_recreate_migrations()

def get_session():
    """
    FastAPI 의존성 주입을 위한 데이터베이스 세션 제너레이터입니다.
    """
    with Session(engine) as session:
        yield session
