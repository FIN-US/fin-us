from sqlalchemy import inspect, text
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
        "tool_verified",
        # DEFAULT 0: 이 컬럼이 없던 시절에 만들어진 기존 행을 "도구 미검증"으로
        # 채운다. NULL이나 1로 채우면 지어낸 과거 리포트가 검증된 것처럼 보일 수
        # 있으므로(#162) 반드시 0(False)이어야 한다.
        "ALTER TABLE agentreport ADD COLUMN tool_verified BOOLEAN NOT NULL DEFAULT 0",
    ),
)


def _run_schema_migrations() -> None:
    """create_all이 다루지 못하는, 기존 테이블에 대한 컬럼 추가를 처리한다."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, column_name, alter_sql in _PENDING_COLUMN_MIGRATIONS:
            if table_name not in existing_tables:
                # create_all이 이미 최신 스키마로 새로 만들었으므로 손댈 필요 없음
                continue
            columns = {col["name"] for col in inspector.get_columns(table_name)}
            if column_name in columns:
                continue
            conn.execute(text(alter_sql))


def init_db():
    """
    데이터베이스 테이블을 초기화합니다.
    """
    SQLModel.metadata.create_all(engine)
    _run_schema_migrations()

def get_session():
    """
    FastAPI 의존성 주입을 위한 데이터베이스 세션 제너레이터입니다.
    """
    with Session(engine) as session:
        yield session
