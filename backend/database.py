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

def get_session():
    """
    FastAPI 의존성 주입을 위한 데이터베이스 세션 제너레이터입니다.
    """
    with Session(engine) as session:
        yield session
