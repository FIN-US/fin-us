from sqlmodel import create_engine, Session, SQLModel
from .config import DATABASE_URL

# SQLite 사용 시 connect_args={"check_same_thread": False}가 필요함
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, echo=True)

def init_db():
    """
    데이터베이스 테이블을 초기화합니다.
    """
    SQLModel.metadata.create_all(engine)

def get_session():
    """
    FastAPI 의존성 주입을 위한 데이터베이스 세션 제너레이터입니다.
    """
    with Session(engine) as session:
        yield session
