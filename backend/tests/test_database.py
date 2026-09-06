from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from ..main import app, get_session
from ..models import Diary, AgentReport, Portfolio, CatalystEvent
from ..scheduler import PRICE_FRESHNESS_TTL

# 이슈 #196: current_price는 값과 **나이**가 함께 있어야 "안다"가 된다. 아래 테스트들이
# 검증하려는 것은 신선도 게이트가 아니라 매핑·집계이므로, 나이는 확실히 신선한 고정
# 시각으로 못 박아 게이트를 통과시킨다. 시각을 고정하는 이유는 price_updated_at이
# 응답에 ISO 문자열로 실려 완전 일치 단언의 일부가 되기 때문이다.
# 고정 리터럴을 쓰지 않는 이유: TTL은 "지금"으로부터의 나이라, 달력 상수는 파일을
# 쓴 날에만 신선하고 그 뒤로는 조용히 낡아 이 테스트들이 게이트 회귀가 아니라 날짜
# 때문에 깨진다. 대신 import 시각으로 한 번만 고정해, 응답에 실릴 ISO 문자열은
# 결정적이면서 값은 항상 신선하게 만든다. 마이크로초는 버려 문자열을 안정시킨다.
_FRESH_AT = datetime.now(timezone.utc).replace(microsecond=0)
_FRESH_ISO = _FRESH_AT.isoformat()

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
            price_updated_at=_FRESH_AT,
        )
    )
    session.add(
        Portfolio(
            stock_code="000660",
            stock_name="SK하이닉스",
            quantity=5,
            avg_price=200000,
            current_price=190000,
            price_updated_at=_FRESH_AT,
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
                "price_updated_at": _FRESH_ISO,
                "price_updated_at_known": True,
            },
            {
                "name": "SK하이닉스",
                "current_price": 190000.0,
                "avg_price": 200000.0,
                "return_rate": -5.0,
                "quantity": 5,
                "price_known": True,
                "return_rate_known": True,
                "price_updated_at": _FRESH_ISO,
                "price_updated_at_known": True,
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
            price_updated_at=_FRESH_AT,
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
                # current_price 자체가 없으므로 갱신 시각도 없다 (#196).
                "price_updated_at": "",
                "price_updated_at_known": False,
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
                "price_updated_at": _FRESH_ISO,
                "price_updated_at_known": True,
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
            price_updated_at=_FRESH_AT,
        )
    )
    session.add(
        Portfolio(
            stock_code="005930",
            stock_name="정상",
            quantity=1,
            avg_price=70000,
            current_price=77000,
            price_updated_at=_FRESH_AT,
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


def test_get_visualization_portfolio_price_known_honours_freshness_ttl(
    client: TestClient,
    session: Session,
):
    """price_known은 current_price의 존재가 아니라 **나이**로 결정된다 (#196).

    세 행을 한 응답 안에 함께 놓는 것이 이 테스트의 핵심이다. 경계 양쪽을 따로 두면
    "항상 False"나 "항상 True"로 되돌리는 회귀가 한쪽 테스트만 깨뜨려 눈에 덜 띈다.

      - 낡음:  now - TTL - 1초  → False (경계 밖)
      - 신선:  now - TTL + 1초  → True  (경계 안)
      - 미상:  price_updated_at IS NULL, current_price는 있음 → False

    세 번째 행이 이 이슈의 본론이다. 이 컬럼이 없던 시절에 저장된 행은 값은 있어도
    언제 채운 값인지 알 수 없다 — 그것을 신선으로 통과시키면 낡은 시세가 현재가로
    나가고, 게이트를 붙인 의미가 사라진다.

    이 테스트가 잡는 mutation:
    - PRICE_FRESHNESS_TTL을 넓히거나 좁히면 아래 상수 단언이 깨진다.
    - is_price_fresh의 비교 방향을 뒤집으면(>= / <) 두 경계 행의 판정이 서로 바뀐다.
    - NULL 분기를 True로 되돌리거나 지우면 "미상" 행이 신선으로 통과한다.
    - main이 price_known을 다시 `current_price is not None`으로 되돌리면 세 행이
      모두 True가 된다.
    """
    from ..scheduler import is_price_fresh

    # 상수 자체를 고정한다. 아래 경계 단언은 PRICE_FRESHNESS_TTL을 기호로 쓰므로
    # 상수를 넓혀도 함께 움직여 통과해 버린다 — 값과 그 근거(주기의 3배)를 여기서
    # 못 박아야 "조용히 넓히기"가 빨간 줄로 드러난다.
    assert PRICE_FRESHNESS_TTL == timedelta(minutes=30)
    assert PRICE_FRESHNESS_TTL == 3 * timedelta(minutes=10), (
        "시세 갱신 주기(market_monitoring, 10분)의 3배라는 근거가 깨졌습니다. "
        "주기를 바꿨다면 scheduler.PRICE_FRESHNESS_TTL의 주석부터 갱신하세요."
    )

    # 경계 판정 자체는 now를 명시해 결정적으로 확인한다(요청 처리 지연에 흔들리지 않게).
    fixed_now = datetime(2026, 9, 3, 6, 0, 0, tzinfo=timezone.utc)
    assert is_price_fresh(fixed_now - PRICE_FRESHNESS_TTL - timedelta(seconds=1), now=fixed_now) is False
    assert is_price_fresh(fixed_now - PRICE_FRESHNESS_TTL + timedelta(seconds=1), now=fixed_now) is True
    assert is_price_fresh(fixed_now - PRICE_FRESHNESS_TTL, now=fixed_now) is True, (
        "정확히 TTL만큼 지난 값은 아직 경계 안이다(<=)"
    )
    assert is_price_fresh(None, now=fixed_now) is False

    now = datetime.now(timezone.utc)
    session.add(
        Portfolio(
            stock_code="005930",
            stock_name="낡음",
            quantity=1,
            avg_price=70000,
            current_price=77000,
            price_updated_at=now - PRICE_FRESHNESS_TTL - timedelta(seconds=1),
        )
    )
    session.add(
        Portfolio(
            stock_code="000660",
            stock_name="신선",
            quantity=1,
            avg_price=190000,
            current_price=200000,
            price_updated_at=now - PRICE_FRESHNESS_TTL + timedelta(seconds=1),
        )
    )
    session.add(
        Portfolio(
            stock_code="035420",
            stock_name="나이미상",
            quantity=1,
            avg_price=180000,
            current_price=200000,
            price_updated_at=None,
        )
    )
    session.commit()

    data = client.get("/api/v1/portfolio").json()["data"]
    holdings = {h["name"]: h for h in data["holdings"]}

    assert holdings["낡음"]["price_known"] is False
    assert holdings["낡음"]["current_price"] is None, (
        "신선하지 않은 값을 current_price로 내리면 소비자가 그것을 현재가로 읽는다"
    )
    assert holdings["낡음"]["price_updated_at_known"] is True, (
        "나이는 알고 있다 — 다만 그 나이가 TTL을 넘겼을 뿐이다"
    )

    assert holdings["신선"]["price_known"] is True
    assert holdings["신선"]["current_price"] == 200000.0

    assert holdings["나이미상"]["price_known"] is False, (
        "값은 있어도 나이를 모르면 신선하다고 단언할 수 없다"
    )
    assert holdings["나이미상"]["price_updated_at"] == ""
    assert holdings["나이미상"]["price_updated_at_known"] is False

    # 신선하지 않은 두 종목이 매입가 추정으로 들어가므로 총자산은 추정값이다.
    assert data["total_asset_is_estimate"] is True
    # 200,000(신선 현재가) + 70,000(낡음 매입가) + 180,000(나이미상 매입가)
    assert data["total_asset"] == 450000.0


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


def test_get_catalysts_to_date_omitted_keeps_no_upper_bound(
    client: TestClient, session: Session, frozen_today: date
):
    # to_date를 생략하면 기존 동작(상한 없음)이 그대로 유지되는지 확인한다.
    today = frozen_today
    session.add(_make_catalyst(stock_name="가까움", event_date=today))
    session.add(_make_catalyst(stock_name="아주먼미래", event_date=today + timedelta(days=365)))
    session.commit()

    response = client.get("/api/v1/db/catalysts")
    assert response.status_code == 200
    names = [row["stock_name"] for row in response.json()["data"]]
    assert names == ["가까움", "아주먼미래"]


def test_get_catalysts_to_date_includes_boundary_excludes_after(
    client: TestClient, session: Session, frozen_today: date
):
    # to_date는 "그 날짜까지(당일 포함)"여야 한다: event_date == to_date는 포함되고,
    # 그 이후는 제외된다.
    today = frozen_today
    session.add(_make_catalyst(stock_name="경계이전", event_date=today + timedelta(days=1)))
    session.add(_make_catalyst(stock_name="경계", event_date=today + timedelta(days=2)))
    session.add(_make_catalyst(stock_name="경계이후", event_date=today + timedelta(days=3)))
    session.commit()

    response = client.get(
        "/api/v1/db/catalysts",
        params={"from_date": today.isoformat(), "to_date": (today + timedelta(days=2)).isoformat()},
    )
    assert response.status_code == 200
    names = [row["stock_name"] for row in response.json()["data"]]
    assert names == ["경계이전", "경계"]


def test_get_catalysts_from_date_equals_to_date_single_day(
    client: TestClient, session: Session, frozen_today: date
):
    # from_date == to_date는 단일 날짜 조회로 유효해야 한다(422가 아니다).
    today = frozen_today
    session.add(_make_catalyst(stock_name="전날", event_date=today - timedelta(days=1)))
    session.add(_make_catalyst(stock_name="당일", event_date=today))
    session.add(_make_catalyst(stock_name="다음날", event_date=today + timedelta(days=1)))
    session.commit()

    response = client.get(
        "/api/v1/db/catalysts",
        params={"from_date": today.isoformat(), "to_date": today.isoformat()},
    )
    assert response.status_code == 200
    names = [row["stock_name"] for row in response.json()["data"]]
    assert names == ["당일"]


def test_get_catalysts_valid_but_empty_range_returns_200_not_422(
    client: TestClient, session: Session, frozen_today: date
):
    """유효한 구간에 이벤트가 0건이면 200 + 빈 배열이어야 한다.

    to_date < from_date를 422로 거부하는 근거는 "이 구간에 이벤트가 없다"와
    "파라미터를 잘못 보냈다"를 클라이언트가 구분할 수 있게 하는 것이다. 그 구분은
    양쪽이 모두 고정돼야 성립하는데, 다른 테스트들은 422(후자)만 검증한다. 대비되는
    쪽이 비어 있으면 훗날 누군가 "빈 구간도 에러로 알려주자"고 바꿔도 스위트가
    red가 되지 않아 422의 존재 이유가 조용히 사라진다. 이 테스트가 그 절반을 고정한다.
    """
    today = frozen_today
    # 조회 구간(내일~모레) 바깥에만 이벤트를 둬, 구간 자체는 유효하지만 결과가 0건이 되게 한다.
    session.add(_make_catalyst(stock_name="구간이전", event_date=today))
    session.add(_make_catalyst(stock_name="구간이후", event_date=today + timedelta(days=10)))
    session.commit()

    response = client.get(
        "/api/v1/db/catalysts",
        params={
            "from_date": (today + timedelta(days=1)).isoformat(),
            "to_date": (today + timedelta(days=2)).isoformat(),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"] == []
    assert body["message"] is None


def test_get_catalysts_rejects_to_date_before_from_date(
    client: TestClient, session: Session, frozen_today: date
):
    # status_code == 422만 확인하면 부족하다: (1) detail을 평문 문자열로 바꿔도
    # 여전히 422라 통과하고 — "이벤트 없음"과 "파라미터 오류"를 구분하려는 이 422의
    # 근거 자체(구조화된 detail)가 무방비가 된다. (2) 같은 엔드포인트에
    # min_length=1/ge=1/le=500 등 FastAPI 자체 검증도 422를 내므로, 우리 검사를
    # 지우고 다른 경로로 422가 나도 구분하지 못한다. detail 내용까지 확인해 두 경우
    # 모두 잡는다.
    # 뮤테이션: detail을 평문 문자열(예: detail="...")로 바꾸면 이 단언이 red가 된다.
    today = frozen_today
    to_date = today - timedelta(days=1)
    response = client.get(
        "/api/v1/db/catalysts",
        params={
            "from_date": today.isoformat(),
            "to_date": to_date.isoformat(),
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    # FastAPI 자체 검증 422와 동일한 list[{loc,msg,type}] 형태다(아래 share_detail_shape 참고).
    assert isinstance(detail, list)
    assert len(detail) == 1
    error = detail[0]
    assert error["loc"] == ["query", "to_date"]
    assert error["type"] == "value_error.date_range"
    assert error["ctx"]["from_date"] == today.isoformat()
    assert error["ctx"]["to_date"] == to_date.isoformat()
    # from_date를 명시해 보냈으므로 기본값 적용이 아니다.
    assert error["ctx"]["from_date_defaulted"] is False


def test_get_catalysts_to_date_only_reports_from_date_defaulted(
    client: TestClient, session: Session, frozen_today: date
):
    # from_date를 생략하면 서버가 KST 오늘로 채운다. 과거 구간을 의도해 to_date만 보낸
    # 클라이언트는 보낸 적 없는 from_date와 비교돼 422를 받으므로, 원인을 알려면
    # "기본값이 적용됐다"는 사실이 detail에 실려야 한다. 이게 없으면 메시지가
    # "to_date must not be earlier than from_date"뿐이라 무엇을 고쳐야 할지 알 수 없다.
    today = frozen_today
    response = client.get(
        "/api/v1/db/catalysts",
        params={"to_date": (today - timedelta(days=30)).isoformat()},
    )
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["ctx"]["from_date_defaulted"] is True
    assert error["ctx"]["from_date"] == today.isoformat()
    # 메시지 자체에도 기본값 적용 사실이 드러나야 한다(ctx를 안 보는 클라이언트/사람용).
    assert "defaulted" in error["msg"]


def test_get_catalysts_custom_and_native_422_share_detail_shape(
    client: TestClient, frozen_today: date
):
    """두 종류의 422가 같은 detail 형태(list)를 갖는지 고정한다.

    to_date < from_date 검사와 FastAPI 자체 검증(예: stock_name의 min_length=1)은
    같은 엔드포인트에서 같은 422를 낸다. 형태가 다르면 클라이언트가
    `for err in resp.json()["detail"]: err["loc"]`처럼 단일 경로로 파싱할 수 없고,
    dict를 순회해 키 문자열에서 TypeError가 난다. 두 경로가 같은 형태임을 실측으로
    묶어 클라이언트의 단일 파싱 경로를 보장한다.

    형태가 같아진 대가로 "다른 경로에서 나온 422를 우리 검사가 통과한 것으로 착각"할
    위험이 생기므로, loc/type으로 두 422를 여전히 구별할 수 있음도 함께 단언한다.
    """
    today = frozen_today
    native = client.get("/api/v1/db/catalysts", params={"stock_name": ""})
    custom = client.get(
        "/api/v1/db/catalysts",
        params={
            "from_date": today.isoformat(),
            "to_date": (today - timedelta(days=1)).isoformat(),
        },
    )
    assert native.status_code == 422
    assert custom.status_code == 422

    native_detail = native.json()["detail"]
    custom_detail = custom.json()["detail"]
    # 단일 파싱 경로: 둘 다 list이고, 원소는 loc/msg/type을 갖는 dict다.
    for detail in (native_detail, custom_detail):
        assert isinstance(detail, list)
        assert detail
        for error in detail:
            assert isinstance(error, dict)
            assert {"loc", "msg", "type"} <= set(error.keys())

    # 형태가 같아도 두 422는 구별 가능해야 한다.
    assert native_detail[0]["loc"] == ["query", "stock_name"]
    assert custom_detail[0]["loc"] == ["query", "to_date"]
    assert native_detail[0]["type"] != custom_detail[0]["type"]
    assert custom_detail[0]["type"] == "value_error.date_range"


def test_get_catalysts_truncated_within_to_date_range(
    client: TestClient, session: Session, frozen_today: date
):
    # to_date가 있어도 구간 안에 limit을 넘는 이벤트가 있으면 여전히 truncated 신호가 떠야 한다.
    today = frozen_today
    for i in range(4):
        session.add(_make_catalyst(stock_name=f"종목{i}", event_date=today + timedelta(days=i)))
    session.commit()

    response = client.get(
        "/api/v1/db/catalysts",
        params={
            "from_date": today.isoformat(),
            "to_date": (today + timedelta(days=10)).isoformat(),
            "limit": 3,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 3
    assert body["message"] == "truncated"


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
