from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlmodel import create_engine, Session, SQLModel
from .config import DATABASE_URL, DB_ECHO

# SQLite 사용 시 connect_args={"check_same_thread": False}가 필요함
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, echo=DB_ECHO)

# 스키마 변경 시 여기에 (테이블명, 컬럼명, 실행할 문장들) 을 추가한다.
# SQLModel.metadata.create_all()은 없는 테이블은 만들지만 기존 테이블에 컬럼을
# 추가하지는 않는다 (SQLAlchemy의 문서화된 동작). 이미 배포된 SQLite 파일은
# create_all만으로는 새 컬럼을 얻지 못하므로, 부팅 시 직접 보강한다.
#
# 세 번째 원소가 문장 하나가 아니라 **문장들**인 이유는 #259의 notified_at 때문이다.
# 컬럼 추가만으로는 기존 행이 "미통지"로 남아 outbox가 과거 체결을 전부 다시 알리는데,
# 그걸 막는 백필은 ALTER와 같은 조건(= 컬럼이 방금 없었을 때)에서 정확히 한 번만 돌아야
# 한다. 부팅마다 무조건 도는 자리에 두면 진짜 미통지 행까지 통지된 것으로 덮는다.
#
# 문장들은 순서대로 실행되지만 **원자적이지 않다**. pysqlite가 DML에서만 암묵적으로
# BEGIN하므로 ALTER는 engine.begin() 안에서도 즉시 커밋된다(_run_table_recreate_migrations의
# 같은 주석 참조). ALTER와 백필 사이에서 프로세스가 죽으면 컬럼만 남고 백필은 영영 돌지
# 않는다 — 다음 부팅은 컬럼이 있어 이 항목을 건너뛰기 때문이다. 그때 과거 행이 미통지로
# 남지만, 재배달 대상은 scheduler.TRADE_NOTIFY_MAX_AGE(기본 24시간) 안의 체결로 한정되므로
# 새어 나가는 통지도 그 창 안으로 유계다.
_PENDING_COLUMN_MIGRATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "agentreport",
        "provider_supports_tools",
        # DEFAULT 0: 이 컬럼이 없던 시절에 만들어진 기존 행을 "도구 미지원"으로
        # 채운다. NULL이나 1로 채우면 지어낸 과거 리포트가 검증된 것처럼 보일 수
        # 있으므로(#162) 반드시 0(False)이어야 한다.
        ("ALTER TABLE agentreport ADD COLUMN provider_supports_tools BOOLEAN NOT NULL DEFAULT 0",),
    ),
    # #298: 신호 점수화. 세 컬럼 모두 NULL 허용이고 DEFAULT를 주지 않는다 —
    # provider_supports_tools와 정반대의 선택이며, 이유도 정반대다. 저쪽은 "구버전
    # 행은 도구를 쓰지 않았다"가 코드로 증명되는 사실이라 0으로 채웠다. 이쪽은
    # 구버전 행이 몇 점이었는지 알 방법이 없다. DEFAULT 0을 주면 "모델이 중립이라고
    # 판단함"이라는 없던 사실이 소급 생성된다(#122·#162의 "0과 모름 구분").
    (
        "agentreport",
        "signal_score",
        ("ALTER TABLE agentreport ADD COLUMN signal_score INTEGER",),
    ),
    (
        "agentreport",
        "signal_reason",
        ("ALTER TABLE agentreport ADD COLUMN signal_reason VARCHAR",),
    ),
    (
        "agentreport",
        "signal_uncertainty",
        ("ALTER TABLE agentreport ADD COLUMN signal_uncertainty FLOAT",),
    ),
    # #259 2단계: 체결 통지 outbox. null = 미통지.
    (
        "tradehistory",
        "notified_at",
        (
            "ALTER TABLE tradehistory ADD COLUMN notified_at DATETIME",
            # 백필. 이 컬럼이 없던 시절의 행은 outbox가 책임진 적이 없다 — 그 행들을
            # 미통지로 두면 배포 직후 첫 주기가 과거 체결을 전부 "통지가 늦었습니다"로
            # 다시 알린다. 그렇다고 "통지됐다"가 증명되는 사실도 아니므로 값을 지어내지
            # 않고 trade_date를 그대로 쓴다: "이 행의 통지 책임은 체결 시점에 끝났다"는
            # 뜻이고, 지연을 재면 0이 나와 소급 채운 행이 눈에 띈다.
            #
            # signal_score를 DEFAULT 없이 null로 둔 것과 반대 방향이지만 기준은 같다.
            # 저쪽은 구버전 행의 점수를 알 방법이 없어 지어내지 않았고, 이쪽은 지어내지
            # 않으면 **없던 통지가 새로 나간다** — null이 무해한 자리가 아니다.
            "UPDATE tradehistory SET notified_at = trade_date WHERE notified_at IS NULL",
        ),
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

    항목 하나가 문장 여러 개일 수 있다(#259의 notified_at은 ALTER + 백필). 늦게 도착한
    워커가 첫 문장에서 "duplicate column name"을 받으면 뒤 문장도 함께 건너뛰는데, 그
    문장들은 먼저 도착한 워커가 이미 실행했으므로 옳다. 원자성 한계는
    _PENDING_COLUMN_MIGRATIONS 주석에 적혀 있다.

    주의: BOOLEAN NOT NULL DEFAULT 0 구문과 "duplicate column name" 문자열 매칭은
    SQLite 전용이다. 다른 방언이면 조용히 오동작할 수 있으므로 가드를 둔다.
    """
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            f"스키마 마이그레이션은 SQLite 전용입니다 (현재: {engine.dialect.name}). "
            "다른 DB로 옮기려면 alembic을 도입하세요."
        )
    for table_name, column_name, statements in _PENDING_COLUMN_MIGRATIONS:
        inspector = inspect(engine)
        if table_name not in inspector.get_table_names():
            # create_all이 이미 최신 스키마로 새로 만들었으므로 손댈 필요 없음
            continue
        columns = {col["name"] for col in inspector.get_columns(table_name)}
        if column_name in columns:
            continue
        try:
            with engine.begin() as conn:
                for statement in statements:
                    conn.execute(text(statement))
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
    "signal_score INTEGER, "            # nullable: 채점 실패(fail-open)와 구버전 행은 null (#298)
    "signal_reason VARCHAR, "           # nullable: 위와 동일
    "signal_uncertainty FLOAT, "        # nullable: 기사 2건 미만이면 흩어짐이 정의되지 않음
    "created_at DATETIME NOT NULL"
    ")"
)
_RECREATE_AGENTREPORT_COLS = (
    "id, stock_code, stock_name, provider, summary, decision, "
    "confidence_score, reason, provider_supports_tools, "
    "signal_score, signal_reason, signal_uncertainty, created_at"
)
# Improvement #1 (#162 리뷰): 재생성 직전 실제 컬럼 집합과 DDL 가정을 비교하는 단언에 쓴다.
# _PENDING_COLUMN_MIGRATIONS에 agentreport 컬럼이 추가되어도 이 집합을 갱신하지 않으면
# 부팅 실패(RuntimeError)로 드러난다 — DDL을 갱신하기 전까지는 재생성을 막는다.
_RECREATE_AGENTREPORT_COL_SET: frozenset[str] = frozenset(
    c.strip() for c in _RECREATE_AGENTREPORT_COLS.split(",")
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

    # Improvement #1 (#162 리뷰): 실제 컬럼 집합이 DDL의 가정과 일치하는지 단언한다.
    # _run_schema_migrations()가 새 컬럼 X를 ALTER로 추가한 뒤 이 함수가 X 없는 테이블을
    # 만들면 X와 그 데이터가 조용히 소실된다. 불일치 시 명시적 부팅 실패로 만든다.
    actual_cols = set(col_info.keys())
    if actual_cols != _RECREATE_AGENTREPORT_COL_SET:
        raise RuntimeError(
            "agentreport 스키마가 재생성 DDL의 가정과 다릅니다. DDL을 갱신하지 않으면 "
            f"차이나는 컬럼이 소실됩니다: DB에만 있음={sorted(actual_cols - _RECREATE_AGENTREPORT_COL_SET)}, "
            f"DDL에만 있음={sorted(_RECREATE_AGENTREPORT_COL_SET - actual_cols)}"
        )

    # decision이 NOT NULL인 경우: 테이블 재생성으로 nullable로 변경
    try:
        with engine.begin() as conn:
            # Critical fix (#162 리뷰): 이전 시도가 중단되어 남겨진 고아 테이블을 먼저 회수한다.
            # pysqlite는 DML에서만 암묵적으로 BEGIN하므로 CREATE TABLE은 engine.begin()
            # 진입 시점에도 autocommit으로 즉시 커밋된다. SIGTERM/OOM kill/디스크 부족 등으로
            # 마이그레이션이 중단되면 agentreport_new가 디스크에 남고, 다음 부팅에서
            # CREATE TABLE "already exists" → 재확인에서 decision이 여전히 NOT NULL →
            # raise → init_db() 실패 → 영구 부팅 불능이 된다.
            # DROP TABLE IF EXISTS는 이 경로를 차단한다. 테이블이 없으면 no-op.
            #
            # 주의: 동시 워커 A가 CREATE TABLE을 커밋한 직후 B가 DROP하면 A의 진행 중
            # 테이블이 사라진다. 현재 배포(단일 프로세스)에서는 해당 없다. 다중 워커가
            # 필요해지면 engine 이벤트로 BEGIN IMMEDIATE를 선점해 직렬화해야 한다.
            conn.execute(text("DROP TABLE IF EXISTS agentreport_new"))
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
