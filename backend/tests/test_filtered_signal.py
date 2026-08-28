"""걸러진 신호 채점 기록의 저장·정리·집계 (#304)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from ..filtered_signal_repo import (
    ScoreBucket,
    SqliteFilteredSignalRepo,
    fill_score_axis,
    score_histogram,
)
from ..config import SIGNAL_SCORE_THRESHOLD
from ..main import app, get_session
from ..models import FilteredSignal


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="repo")
def repo_fixture(session: Session):
    """테스트 세션을 그대로 쓰는 repo.

    repo는 세션 팩토리를 with로 열고 닫는다. 인메모리 DB에서 그 세션이 닫히면
    테스트가 결과를 확인할 방법이 사라지므로, 닫히지 않는 래퍼를 돌려준다.
    """

    class _KeepOpenSession:
        def __enter__(inner):
            return session

        def __exit__(inner, *exc):
            return False

    return SqliteFilteredSignalRepo(lambda: _KeepOpenSession())


@pytest.fixture(name="client")
def client_fixture(session: Session):
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _add(session: Session, **kwargs) -> FilteredSignal:
    defaults = dict(stock_name="삼성전자", source="news", score=1, threshold=2)
    defaults.update(kwargs)
    row = FilteredSignal(**defaults)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_record_persists_score_and_threshold(repo, session: Session):
    """점수·근거·흩어짐과 함께 그 시점의 임계값이 남아야 한다.

    임계값을 남기지 않으면 나중에 설정을 바꾼 뒤 과거 행이 왜 걸러졌는지 설명할 수
    없다 — 같은 1점이 임계값 2에서는 걸러진 신호이고 1에서는 통과한 신호다.
    """
    saved = await repo.record(
        stock_name="SK하이닉스",
        source="disclosure",
        score=-1,
        threshold=2,
        reason="단순 정정 공시",
        uncertainty=0.5,
    )

    assert saved is not None
    rows = session.exec(select(FilteredSignal)).all()
    assert len(rows) == 1
    assert rows[0].stock_name == "SK하이닉스"
    assert rows[0].source == "disclosure"
    assert rows[0].score == -1
    assert rows[0].threshold == 2
    assert rows[0].reason == "단순 정정 공시"
    assert rows[0].uncertainty == 0.5


@pytest.mark.asyncio
async def test_record_skips_unscored_signal(repo, session: Session):
    """채점이 없었던 신호(빈 본문·직전과 동일)는 행을 만들지 않는다.

    점수가 없는 행은 분포에 보탤 정보가 0인데, 감시 루프가 종목·소스마다 10분 주기로
    도는 탓에 그런 행만으로 테이블이 가득 찬다.
    """
    saved = await repo.record(
        stock_name="삼성전자", source="news", score=None, threshold=2
    )

    assert saved is None
    assert session.exec(select(FilteredSignal)).all() == []


@pytest.mark.asyncio
async def test_purge_expired_removes_only_old_rows(repo, session: Session):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    # id는 삭제 전에 읽어 둔다. 커밋으로 만료된 속성을 삭제 후에 읽으면 SQLAlchemy가
    # 사라진 행을 다시 읽으려다 ObjectDeletedError로 죽는다.
    fresh_id = _add(session, created_at=now - timedelta(days=29, hours=23)).id
    stale_id = _add(session, created_at=now - timedelta(days=31)).id

    deleted = await repo.purge_expired(30, now=now)

    assert deleted == 1
    remaining = {row.id for row in session.exec(select(FilteredSignal)).all()}
    assert remaining == {fresh_id}
    assert stale_id not in remaining


@pytest.mark.asyncio
async def test_purge_expired_is_idempotent(repo, session: Session):
    """두 번째 정리는 0건이어야 한다 — 워커가 둘이어도 서로를 망가뜨리지 않는다."""
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    _add(session, created_at=now - timedelta(days=40))

    assert await repo.purge_expired(30, now=now) == 1
    assert await repo.purge_expired(30, now=now) == 0


def test_score_histogram_counts_by_score(session: Session):
    _add(session, score=1)
    _add(session, score=1)
    _add(session, score=0)
    _add(session, score=-1)

    histogram = score_histogram(session)

    assert histogram.total == 4
    assert histogram.buckets == (
        ScoreBucket(score=-1, count=1),
        ScoreBucket(score=0, count=1),
        ScoreBucket(score=1, count=2),
    )
    assert histogram.thresholds == (2,)


def test_score_histogram_filters_by_source_and_stock(session: Session):
    _add(session, source="news", stock_name="삼성전자", score=1)
    _add(session, source="disclosure", stock_name="삼성전자", score=1)
    _add(session, source="news", stock_name="NAVER", score=0)

    by_source = score_histogram(session, source="news")
    assert by_source.total == 2

    by_stock = score_histogram(session, source="news", stock_name="NAVER")
    assert by_stock.total == 1
    assert by_stock.buckets == (ScoreBucket(score=0, count=1),)


def test_score_histogram_filters_by_since(session: Session):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    _add(session, score=1, created_at=now - timedelta(days=10))
    _add(session, score=0, created_at=now - timedelta(days=1))

    recent = score_histogram(session, since=now - timedelta(days=7))

    assert recent.total == 1
    assert recent.buckets == (ScoreBucket(score=0, count=1),)


def test_score_histogram_reports_mixed_thresholds(session: Session):
    """구간에 임계값이 둘 이상 섞였다면 그 사실이 드러나야 한다.

    임계값이 바뀐 지점을 걸쳐 집계하면 서로 다른 설정에서 걸러진 신호가 한 분포로
    보인다. 그걸 모른 채 읽으면 임계값 조정 근거가 오염된다.
    """
    _add(session, score=1, threshold=2)
    _add(session, score=2, threshold=3)

    assert score_histogram(session).thresholds == (2, 3)


def test_score_histogram_on_empty_table(session: Session):
    histogram = score_histogram(session)

    assert histogram.total == 0
    assert histogram.buckets == ()
    assert histogram.thresholds == ()
    assert histogram.oldest is None
    assert histogram.newest is None


def test_fill_score_axis_fills_unseen_scores_with_zero():
    filled = fill_score_axis((ScoreBucket(score=1, count=3),), -3, 3)

    assert [bucket.score for bucket in filled] == [-3, -2, -1, 0, 1, 2, 3]
    assert [bucket.count for bucket in filled] == [0, 0, 0, 0, 3, 0, 0]


def test_histogram_endpoint_returns_full_axis(client: TestClient, session: Session):
    _add(session, score=1)
    _add(session, score=1)
    _add(session, score=-1)

    response = client.get("/api/v1/db/filtered-signals/histogram")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 3
    # 응답의 threshold는 지금 적용 중인 설정값이다 (기록 시점의 값은 recorded_thresholds).
    assert data["threshold"] == SIGNAL_SCORE_THRESHOLD
    assert data["recorded_thresholds"] == [2]
    assert data["window_days"] is None
    assert data["since"] is None
    assert [bucket["score"] for bucket in data["buckets"]] == [-3, -2, -1, 0, 1, 2, 3]
    assert [bucket["count"] for bucket in data["buckets"]] == [0, 0, 1, 0, 2, 0, 0]


def test_histogram_endpoint_filters(client: TestClient, session: Session):
    _add(session, source="news", stock_name="삼성전자", score=1)
    _add(session, source="disclosure", stock_name="삼성전자", score=0)

    response = client.get(
        "/api/v1/db/filtered-signals/histogram",
        params={"source": "news", "stock_name": "삼성전자"},
    )

    data = response.json()["data"]
    assert data["total"] == 1
    assert [bucket["count"] for bucket in data["buckets"]] == [0, 0, 0, 0, 1, 0, 0]


def test_histogram_endpoint_days_window(client: TestClient, session: Session):
    now = datetime.now(timezone.utc)
    _add(session, score=1, created_at=now - timedelta(days=45))
    _add(session, score=0, created_at=now - timedelta(hours=1))

    response = client.get("/api/v1/db/filtered-signals/histogram", params={"days": 7})

    data = response.json()["data"]
    assert data["total"] == 1
    assert data["window_days"] == 7
    assert data["since"] is not None


def test_histogram_endpoint_rejects_out_of_range_days(client: TestClient):
    assert (
        client.get(
            "/api/v1/db/filtered-signals/histogram", params={"days": 0}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/db/filtered-signals/histogram", params={"days": 366}
        ).status_code
        == 422
    )
