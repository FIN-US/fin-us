import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from ..main import app, get_session
from ..models import Diary, AgentReport, Portfolio

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


def test_get_visualization_portfolio_empty(client: TestClient):
    response = client.get("/api/v1/portfolio")
    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "data": {
            "total_asset": 0,
            "total_return_rate": 0.0,
            "holdings": [],
        },
        "message": None,
    }


def test_get_visualization_portfolio_maps_holdings(
    client: TestClient,
    session: Session,
):
    session.add(
        Portfolio(
            stock_code="005930",
            stock_name="삼성전자",
            quantity=10,
            avg_price=70000,
            current_price=77000,
        )
    )
    session.add(
        Portfolio(
            stock_code="000660",
            stock_name="SK하이닉스",
            quantity=5,
            avg_price=200000,
            current_price=190000,
        )
    )
    session.commit()

    response = client.get("/api/v1/portfolio")

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["data"] == {
        "total_asset": 1720000.0,
        "total_return_rate": 1.1765,
        "holdings": [
            {
                "name": "삼성전자",
                "current_price": 77000.0,
                "avg_price": 70000.0,
                "return_rate": 10.0,
                "quantity": 10,
            },
            {
                "name": "SK하이닉스",
                "current_price": 190000.0,
                "avg_price": 200000.0,
                "return_rate": -5.0,
                "quantity": 5,
            },
        ],
    }


def test_get_visualization_portfolio_handles_missing_prices(
    client: TestClient,
    session: Session,
):
    session.add(
        Portfolio(
            stock_code="035720",
            stock_name="카카오",
            quantity=3,
            avg_price=42000,
            current_price=None,
        )
    )
    session.add(
        Portfolio(
            stock_code="000000",
            stock_name="평단가없음",
            quantity=2,
            avg_price=0,
            current_price=1000,
        )
    )
    session.commit()

    response = client.get("/api/v1/portfolio")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "total_asset": 128000.0,
        "total_return_rate": 0.0,
        "holdings": [
            {
                "name": "카카오",
                "current_price": 42000.0,
                "avg_price": 42000.0,
                "return_rate": 0.0,
                "quantity": 3,
            },
            {
                "name": "평단가없음",
                "current_price": 1000.0,
                "avg_price": 0.0,
                "return_rate": 0.0,
                "quantity": 2,
            },
        ],
    }


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

    async def mock_run_mcp_tool(*args, **kwargs):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr("backend.services.llm_chat", mock_llm_chat)
    monkeypatch.setattr("backend.services.analysis_from_nat_text", mock_analysis_from_nat_text)
    monkeypatch.setattr("backend.services.run_mcp_tool", mock_run_mcp_tool)

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
    assert reports[0].stock_code == "005930"
