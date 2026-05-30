import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from ..main import app, get_session
from ..models import Diary, AgentReport

# 테스트용 인메모리 SQLite 엔진 설정
@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_get_portfolio_empty(client: TestClient):
    response = client.get("/api/v1/db/portfolio")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["data"] == []


def test_create_and_get_diary(client: TestClient):
    # Create
    response = client.post(
        "/api/v1/db/diary",
        json={"title": "Test Title", "content": "Test Content"}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["title"] == "Test Title"
    assert data["content"] == "Test Content"
    assert "id" in data

    # Get
    response = client.get("/api/v1/db/diary")
    assert response.status_code == 200
    diaries = response.json()["data"]
    assert len(diaries) == 1
    assert diaries[0]["title"] == "Test Title"


def test_get_trades_empty(client: TestClient):
    response = client.get("/api/v1/db/trades")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["data"] == []


@pytest.mark.asyncio
async def test_analyze_stock_saves_report(client: TestClient, session: Session, monkeypatch):
    # Mocking llm_chat and analysis_from_nat_text
    async def mock_llm_chat(*args, **kwargs):
        return "mocked raw response"

    def mock_analysis_from_nat_text(raw_text, stock):
        return {
            "summary": f"Summary for {stock}",
            "details": {
                "decision": "BUY",
                "confidence_score": 0.85,
                "reason": "Strong momentum"
            }
        }

    monkeypatch.setattr("backend.services.llm_chat", mock_llm_chat)
    monkeypatch.setattr("backend.services.analysis_from_nat_text", mock_analysis_from_nat_text)

    # API Call
    response = client.get("/api/v1/analyze?stock=삼성전자")
    assert response.status_code == 200
    assert response.json()["status"] == "success"

    # Verify DB save
    reports = session.query(AgentReport).all()
    assert len(reports) == 1
    assert reports[0].stock_name == "삼성전자"
    assert reports[0].decision == "BUY"
    assert reports[0].confidence_score == 0.85
