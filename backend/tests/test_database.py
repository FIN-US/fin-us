from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from ..main import app, get_session
from ..models import Diary, AgentReport, Portfolio, CatalystEvent

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
            "total_asset_is_estimate": False,
            "total_return_rate": 0.0,
            "total_return_rate_known": True,
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
        "total_asset_is_estimate": False,
        "total_return_rate": 1.1765,
        "total_return_rate_known": True,
        "holdings": [
            {
                "name": "삼성전자",
                "current_price": 77000.0,
                "avg_price": 70000.0,
                "return_rate": 10.0,
                "quantity": 10,
                "price_known": True,
                "return_rate_known": True,
            },
            {
                "name": "SK하이닉스",
                "current_price": 190000.0,
                "avg_price": 200000.0,
                "return_rate": -5.0,
                "quantity": 5,
                "price_known": True,
                "return_rate_known": True,
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
        # 카카오 current_price=None → total_asset에 avg_price*qty(126,000)로 포함.
        # 평단가없음 current_price=1000 → total_asset에 1000*2(2,000)로 포함.
        "total_asset": 128000.0,
        # 카카오의 current_price=None으로 인해 total_asset이 추정값이다.
        "total_asset_is_estimate": True,
        # 카카오는 current_price 없어 return_rate 미반영.
        # 평단가없음은 current_price=1000을 알지만 avg_price=0 이라 수익률 계산 불가.
        # → price_known=True(현재가 실제 값), return_rate_known=False(수익률 계산 불가).
        # 매입가를 모르면 수익률도 모른다 — 0%로 단언하지 않는다(이슈 #122).
        # → total_cost=0. 보유 종목은 있으므로 "모름"이고 실제 0%가 아니다 → null.
        "total_return_rate": None,
        "total_return_rate_known": False,
        "holdings": [
            {
                "name": "카카오",
                # current_price=None → null. avg_price 대체값 42000을 반환하던
                # 기존 동작은 "실제 0%" 수익률과 구분이 불가능해 이슈 #122에서 수정됨.
                "current_price": None,
                "avg_price": 42000.0,
                "return_rate": None,
                "quantity": 3,
                "price_known": False,
                "return_rate_known": False,
            },
            {
                "name": "평단가없음",
                "current_price": 1000.0,
                "avg_price": 0.0,
                # current_price는 있으므로 price_known=True.
                # avg_price=0 → _portfolio_return_rate가 None 반환 → return_rate_known=False.
                # 0.00%로 단언하면 이슈 #122가 해소한 문제가 avg_price 경로에서 재현된다.
                "return_rate": None,
                "quantity": 2,
                "price_known": True,
                "return_rate_known": False,
            },
        ],
    }


def test_get_visualization_portfolio_total_return_rate_is_null_when_no_current_price(
    client: TestClient,
    session: Session,
):
    """보유 종목은 있는데 현재가가 하나도 없으면 총수익률은 0.0이 아니라 null이어야 한다.

    현재 Portfolio 동기화는 current_price를 채울 소스가 없어 항상 null로 저장한다.
    이때 total_return_rate로 0.0을 돌려주면 소비자는 그것을 실제 수익률 0%로 읽고,
    종목별 return_rate를 null로 만들어 얻은 구분이 계정 총계에서 그대로 무너진다.

    이 테스트가 잡는 mutation: total_cost == 0 분기에서 None 대신 0.0 반환.
    """
    session.add(
        Portfolio(stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000, current_price=None)
    )
    session.add(
        Portfolio(stock_code="035720", stock_name="카카오", quantity=3, avg_price=42000, current_price=None)
    )
    session.commit()

    data = client.get("/api/v1/portfolio").json()["data"]

    assert data["total_return_rate"] is None, "현재가를 모르는데 0%로 단언하면 안 됩니다"
    assert all(h["return_rate"] is None for h in data["holdings"])
    # 총자산은 매입금액 근사값으로 계산된다: 70000*10 + 42000*3
    assert data["total_asset"] == 826000.0


def test_get_visualization_portfolio_price_known_independent_of_return_rate(
    client: TestClient,
    session: Session,
):
    """current_price를 알지만 avg_price=0인 종목은 price_known=True·return_rate_known=False여야 한다.

    리뷰어 실측 케이스(avg_price=0.0, current_price=50000.0, quantity=10 + 정상 종목 1건).
    이전 구현(price_known = return_rate is not None)에서는 현재가를 아는데도 price_known=False가
    됐고, GetWeight에서 avg_price(0) × quantity / total_asset = 0%로 계산돼 총자산의 대부분을
    차지하는 종목이 비중 차트에서 사라졌다(이슈 #122 연관 결함).

    이 테스트가 잡는 mutation:
    - price_known = True 를 return_rate is not None으로 되돌리면 price_known=False가 돼 실패한다.
    - return_rate_known을 price_known과 동일하게 묶으면 return_rate_known=False가 돼 실패한다.
    """
    session.add(
        Portfolio(
            stock_code="000000",
            stock_name="평단가손실",
            quantity=10,
            avg_price=0,
            current_price=50000,
        )
    )
    session.add(
        Portfolio(
            stock_code="005930",
            stock_name="정상",
            quantity=1,
            avg_price=70000,
            current_price=77000,
        )
    )
    session.commit()

    response = client.get("/api/v1/portfolio")
    assert response.status_code == 200
    data = response.json()["data"]

    # 577,000 = 50,000×10 + 77,000×1
    assert data["total_asset"] == 577000.0

    holdings_by_name = {h["name"]: h for h in data["holdings"]}

    # 평단가손실: current_price 실제 값 → price_known=True. avg_price=0 → return_rate_known=False.
    lost = holdings_by_name["평단가손실"]
    assert lost["price_known"] is True, "현재가를 아는 종목은 price_known=True여야 합니다"
    assert lost["return_rate_known"] is False, "avg_price=0이면 수익률을 계산할 수 없습니다"
    assert lost["current_price"] == 50000.0
    assert lost["return_rate"] is None

    # 정상 종목: 둘 다 True
    normal = holdings_by_name["정상"]
    assert normal["price_known"] is True
    assert normal["return_rate_known"] is True


def test_get_visualization_portfolio_empty_keeps_zero_total_return_rate(client: TestClient):
    """보유 종목이 아예 없으면 "모른다"고 할 것이 없으므로 기존대로 0.0을 유지한다."""
    data = client.get("/api/v1/portfolio").json()["data"]

    assert data["holdings"] == []
    assert data["total_return_rate"] == 0.0


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


def _make_catalyst(
    *,
    stock_name: str,
    event_date: date,
    stock_code: str = "005930",
    event_type: str = "earnings",
    description: str = "실적 발표",
) -> CatalystEvent:
    return CatalystEvent(
        stock_name=stock_name,
        stock_code=stock_code,
        event_type=event_type,
        event_date=event_date,
        description=description,
    )


@pytest.fixture(name="frozen_today")
def frozen_today_fixture(monkeypatch: pytest.MonkeyPatch) -> date:
    # 서버(backend.main.today_kst)와 테스트 픽스처가 같은 "오늘"을 보게 고정한다.
    # 픽스처를 date.today()(테스트 실행 머신의 로컬 타임존)로 만들고 서버는
    # today_kst()(KST)를 쓰면, 두 시계가 KST 00:00~08:59에 하루 어긋나 CI(UTC 러너)에서
    # 픽스처가 필터에서 통째로 빠지는 회귀가 생긴다. 이 픽스처로 그 어긋남을 없앤다.
    fixed = date(2026, 5, 20)
    monkeypatch.setattr("backend.main.today_kst", lambda: fixed)
    return fixed


def test_get_catalysts_empty(client: TestClient):
    response = client.get("/api/v1/db/catalysts")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["data"] == []


def test_get_catalysts_filters_by_stock_name(client: TestClient, session: Session, frozen_today: date):
    today = frozen_today
    session.add(_make_catalyst(stock_name="삼성전자", event_date=today))
    session.add(_make_catalyst(stock_name="SK하이닉스", event_date=today))
    session.commit()

    response = client.get("/api/v1/db/catalysts", params={"stock_name": "삼성전자"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 1
    assert data[0]["stock_name"] == "삼성전자"


def test_get_catalysts_from_date_excludes_past_events(client: TestClient, session: Session):
    today = date.today()
    session.add(_make_catalyst(stock_name="과거", event_date=today - timedelta(days=1)))
    session.add(_make_catalyst(stock_name="오늘", event_date=today))
    session.add(_make_catalyst(stock_name="미래", event_date=today + timedelta(days=1)))
    session.commit()

    response = client.get("/api/v1/db/catalysts", params={"from_date": today.isoformat()})
    assert response.status_code == 200
    names = [row["stock_name"] for row in response.json()["data"]]
    assert names == ["오늘", "미래"]


def test_get_catalysts_limit_boundary(client: TestClient, session: Session, frozen_today: date):
    today = frozen_today
    for i in range(5):
        session.add(
            _make_catalyst(
                stock_name=f"종목{i}",
                event_date=today + timedelta(days=i),
            )
        )
    session.commit()

    response = client.get("/api/v1/db/catalysts", params={"limit": 3})
    assert response.status_code == 200
    data = response.json()["data"]
    assert [row["stock_name"] for row in data] == ["종목0", "종목1", "종목2"]


def test_get_catalysts_sorted_by_event_date_ascending(client: TestClient, session: Session, frozen_today: date):
    today = frozen_today
    session.add(_make_catalyst(stock_name="가장늦음", event_date=today + timedelta(days=10)))
    session.add(_make_catalyst(stock_name="가장이름", event_date=today))
    session.add(_make_catalyst(stock_name="중간", event_date=today + timedelta(days=5)))
    session.commit()

    response = client.get("/api/v1/db/catalysts")
    assert response.status_code == 200
    names = [row["stock_name"] for row in response.json()["data"]]
    assert names == ["가장이름", "중간", "가장늦음"]


def test_get_catalysts_ties_break_by_event_type_then_id(
    client: TestClient, session: Session, frozen_today: date
):
    # event_date만으로 정렬하면 같은 날짜 이벤트 간 순서가 SQL상 보장되지 않아 limit
    # 절단이 비결정적일 수 있다. event_type 오름차순 tie-break와, event_type까지 같을 때
    # id(삽입 순서) tie-break가 둘 다 동작하는지 확인한다.
    today = frozen_today
    # event_type이 다른 경우: 삽입 순서(C, A, B)를 event_type 오름차순(A, B, C)과
    # 반대로 둬, id로만 정렬되는 경우와 구분되게 한다.
    session.add(_make_catalyst(stock_name="C", event_date=today, event_type="c_type"))
    session.add(_make_catalyst(stock_name="A", event_date=today, event_type="a_type"))
    session.add(_make_catalyst(stock_name="B", event_date=today, event_type="b_type"))
    # event_date, event_type이 모두 같은 경우: id(삽입 순서)로 tie-break되는지 확인.
    # stock_name은 삽입 순서와 반대로 둬, id가 아닌 다른 기준으로 정렬되면 실패하게 한다.
    session.add(_make_catalyst(stock_name="First", event_date=today, event_type="a_type"))
    session.add(_make_catalyst(stock_name="Second", event_date=today, event_type="a_type"))
    session.commit()

    response = client.get("/api/v1/db/catalysts")
    assert response.status_code == 200
    names = [row["stock_name"] for row in response.json()["data"]]
    assert names == ["A", "First", "Second", "B", "C"]


def test_get_catalysts_order_by_includes_id_as_final_tie_break(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
):
    # SQLite는 이 테이블(rowid 테이블, id가 곧 rowid)에서 물리적 저장 순서가 이미
    # id 오름차순이고 정렬기도 안정적이라, id를 order_by에서 빼도 지금 규모의 인메모리
    # 테스트 데이터로는 "우연히" 같은 결과가 나와 동작 테스트로는 그 뮤테이션을 잡지
    # 못한다(위 tie-break 테스트가 실제로 보여준 한계). 그래서 실행 결과 대신 쿼리가
    # id까지 명시적으로 order_by에 넣도록 "요청"하는지를 화이트박스로 고정한다 —
    # 우연한 안정성이 아니라 계약으로 결정성을 보장한다.
    captured_sql: list[str] = []
    original_exec = session.exec

    def spy_exec(statement, *args, **kwargs):
        captured_sql.append(str(statement))
        return original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(session, "exec", spy_exec)

    response = client.get("/api/v1/db/catalysts")
    assert response.status_code == 200
    assert captured_sql, "session.exec가 호출되지 않았다"

    sql = captured_sql[0]
    # ORDER BY가 통째로 사라진 뮤턴트에서는 split이 IndexError로 죽어 red의 원인이
    # 드러나지 않는다. 먼저 단언해 실패 메시지에 실제 SQL이 남게 한다.
    assert "ORDER BY" in sql, f"ORDER BY 절이 없다: {sql}"
    order_by_clause = sql.split("ORDER BY", 1)[1]
    assert "catalystevent.event_date" in order_by_clause
    assert "catalystevent.event_type" in order_by_clause
    assert "catalystevent.id" in order_by_clause
    # id가 마지막 tie-break여야 한다: event_type보다 뒤에 나와야 한다.
    assert order_by_clause.index("catalystevent.event_type") < order_by_clause.index(
        "catalystevent.id"
    )


def test_get_catalysts_defaults_to_today_excluding_past(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
):
    # from_date를 생략했을 때의 기본값(KST 기준 오늘)을 고정한다. 위 from_date 테스트는
    # 값을 명시해서 호출하므로 기본값이 date.min 등으로 바뀌어도 통과한다 — 지난 촉매가
    # 시간 링에 섞여 들어오는 회귀를 이 테스트가 잡는다.
    #
    # today_kst()를 고정값으로 monkeypatch한다: 서버가 date.today()(로컬 타임존)로
    # 되돌아가도 이 테스트를 실행하는 개발자 머신이 KST라면 우연히 통과해버려
    # 회귀를 못 잡는다. 고정 날짜로 기대값과 서버 계산을 분리해야 그 회귀가 드러난다.
    fixed = date(2026, 5, 20)
    monkeypatch.setattr("backend.main.today_kst", lambda: fixed)
    session.add(_make_catalyst(stock_name="지난주", event_date=fixed - timedelta(days=7)))
    session.add(_make_catalyst(stock_name="오늘", event_date=fixed))
    session.commit()

    response = client.get("/api/v1/db/catalysts")
    assert response.status_code == 200
    names = [row["stock_name"] for row in response.json()["data"]]
    assert names == ["오늘"]


@pytest.mark.parametrize("limit", [0, 501])
def test_get_catalysts_rejects_out_of_range_limit(client: TestClient, limit: int):
    response = client.get("/api/v1/db/catalysts", params={"limit": limit})
    assert response.status_code == 422


def test_get_catalysts_rejects_empty_stock_name(client: TestClient):
    # 빈 문자열은 "필터 없음"이 아니라 "빈 종목명" 필터로 잘못 해석되어 항상 0건을
    # 반환할 수 있다. 프론트가 "필터 없음"을 빈 문자열로 보내는 실수를 422로 조기에
    # 드러낸다.
    response = client.get("/api/v1/db/catalysts", params={"stock_name": ""})
    assert response.status_code == 422


def test_get_catalysts_not_truncated_when_within_limit(
    client: TestClient, session: Session, frozen_today: date
):
    today = frozen_today
    session.add(_make_catalyst(stock_name="종목0", event_date=today))
    session.commit()

    response = client.get("/api/v1/db/catalysts", params={"limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 1
    assert body["message"] is None


def test_get_catalysts_signals_truncation_when_over_limit(
    client: TestClient, session: Session, frozen_today: date
):
    today = frozen_today
    for i in range(4):
        session.add(_make_catalyst(stock_name=f"종목{i}", event_date=today + timedelta(days=i)))
    session.commit()

    response = client.get("/api/v1/db/catalysts", params={"limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 3
    assert body["message"] == "truncated"


def test_get_catalysts_not_truncated_at_exact_limit(
    client: TestClient, session: Session, frozen_today: date
):
    # 정확히 limit개일 때가 유일하게 애매한 경계다. 위 두 테스트는 각각 경계에서
    # 2 모자라고 1 넘어서 비켜 가므로, len(rows) > limit을 >= 로 바꾼 오프바이원
    # 뮤턴트가 둘 다 통과한다. 이 경계가 깨지면 이벤트가 정확히 limit개일 때 프론트가
    # "더 있음"으로 오해해 없는 다음 페이지를 그린다 — 데이터는 멀쩡한데 UI만 틀린다.
    today = frozen_today
    for i in range(3):
        session.add(_make_catalyst(stock_name=f"종목{i}", event_date=today + timedelta(days=i)))
    session.commit()

    response = client.get("/api/v1/db/catalysts", params={"limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 3
    assert body["message"] is None


@pytest.mark.asyncio
async def test_analyze_stock_saves_report(client: TestClient, session: Session, monkeypatch):
    # A (#162): openai는 도구 없는 경로(_build_toolless_prompt + _analysis_from_toolless_text)를
    # 타므로 analysis_from_nat_text는 호출되지 않는다. llm_chat만 mocking한다.
    async def mock_llm_chat(*args, **kwargs):
        return "mocked raw response"

    async def mock_run_mcp_tool(*args, **kwargs):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr("backend.services.llm_chat", mock_llm_chat)
    monkeypatch.setattr("backend.services.run_mcp_tool", mock_run_mcp_tool)

    # API Call
    response = client.get("/api/v1/analyze?stock=삼성전자")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    payload = response.json()["data"]
    # /api/v1/analyze 응답 payload 자체에도 provider/provider_supports_tools가
    # 실려야 한다 (#162 HIGH2) - AnalysisReport만 반환하고 저장된 값을 붙이지
    # 않으면 이 두 줄이 깨진다.
    assert payload["provider"] == "openai"
    assert payload["provider_supports_tools"] is False
    # A (#162): 도구 없는 provider는 매매 판단을 생성하지 않는다.
    assert payload.get("details") is None

    # Verify DB save
    reports = session.query(AgentReport).all()
    assert len(reports) == 1
    assert reports[0].stock_name == "삼성전자"
    # A (#162): 도구 없는 provider는 decision/confidence_score를 null로 저장한다.
    assert reports[0].decision is None
    assert reports[0].confidence_score is None
    assert reports[0].stock_code == "005930"
    # /api/v1/analyze의 기본 provider(openai)는 tools 없이 모델을 그대로 호출하므로
    # (#162) 저장되는 리포트는 도구를 쓸 수 없는 provider로 표시되어야 한다.
    assert reports[0].provider_supports_tools is False


@pytest.mark.asyncio
async def test_analyze_stock_via_nat_marks_report_tool_supporting(client: TestClient, session: Session, monkeypatch):
    """provider=nat 경로는 도구를 호출할 수 있는 경로로 라우팅되므로
    provider_supports_tools=True로 저장되고 응답 payload에도 실려야 한다.
    (#162 수용 기준: provider=nat 경로의 동작은 불변이어야 한다.) True는 이번
    호출에서 실제로 도구가 호출됐다는 관측이 아니라 provider 능력 신호일 뿐이다
    (#152가 열려 있는 한 그렇다) - 그래도 provider=nat이면 반드시 True로
    기록되어야 한다는 점은 이 테스트가 고정한다.
    """
    async def mock_llm_chat(*args, **kwargs):
        return "mocked raw response"

    def mock_analysis_from_nat_text(raw_text, stock):
        return {
            "summary": f"Summary for {stock}",
            "details": {
                "decision": "HOLD",
                "confidence_score": 0.5,
                "reason": "no strong signal",
            },
        }

    async def mock_run_mcp_tool(*args, **kwargs):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr("backend.services.llm_chat", mock_llm_chat)
    monkeypatch.setattr("backend.services.analysis_from_nat_text", mock_analysis_from_nat_text)
    monkeypatch.setattr("backend.services.run_mcp_tool", mock_run_mcp_tool)

    response = client.get("/api/v1/analyze?stock=삼성전자&provider=nat")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["provider"] == "nat"
    assert payload["provider_supports_tools"] is True

    reports = session.query(AgentReport).all()
    assert len(reports) == 1
    assert reports[0].provider == "nat"
    assert reports[0].provider_supports_tools is True


@pytest.mark.asyncio
async def test_db_reports_round_trips_provider_supports_tools_field(client: TestClient, session: Session, monkeypatch):
    """GET /api/v1/db/reports 응답 JSON에 provider_supports_tools 필드가 실제 값
    그대로 포함되어야 한다. AgentReport에 필드를 추가했지만 API 응답 스키마에서
    빠뜨리면 이 테스트가 깨진다.
    """
    async def mock_llm_chat(*args, **kwargs):
        return "mocked raw response"

    def mock_analysis_from_nat_text(raw_text, stock):
        return {
            "summary": f"Summary for {stock}",
            "details": {
                "decision": "SELL",
                "confidence_score": 0.3,
                "reason": "약세 신호",
            },
        }

    async def mock_run_mcp_tool(*args, **kwargs):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr("backend.services.llm_chat", mock_llm_chat)
    monkeypatch.setattr("backend.services.analysis_from_nat_text", mock_analysis_from_nat_text)
    monkeypatch.setattr("backend.services.run_mcp_tool", mock_run_mcp_tool)

    # openai(기본값, 도구 미지원)와 nat(도구 경로) 둘 다 저장
    assert client.get("/api/v1/analyze?stock=삼성전자").status_code == 200
    assert client.get("/api/v1/analyze?stock=삼성전자&provider=nat").status_code == 200

    response = client.get("/api/v1/db/reports")
    assert response.status_code == 200
    reports = response.json()["data"]
    assert len(reports) == 2

    by_provider = {r["provider"]: r for r in reports}
    assert by_provider["openai"]["provider_supports_tools"] is False
    assert by_provider["nat"]["provider_supports_tools"] is True
