"""임계값 미만으로 걸러진 신호의 채점 기록 저장·정리·집계 (#304).

저장·정리는 감시 루프(scheduler)가 자기 세션으로 쓰므로 세션 팩토리를 받는 repo
클래스가 맡고, 집계는 요청 세션 위에서 도는 API가 쓰므로 세션을 인자로 받는 함수로
둔다 — /api/v1/db/* 라우트들이 이미 요청 세션에 직접 질의하는 방식과 맞춘다.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence, cast

from sqlalchemy import CursorResult, delete, func
from sqlmodel import Session, col, select

from .models import FilteredSignal

logger = logging.getLogger(__name__)


def _as_utc_naive(value: datetime) -> datetime:
    """tz-aware 값을 UTC로 옮기고 tzinfo를 떼어 낸다.

    SQLite의 DATETIME 컬럼은 오프셋을 저장하지 않는다 — SQLAlchemy가 bind 시점에
    tzinfo를 버리므로, 저장된 created_at은 UTC 벽시계 시각의 naive 값이다. 비교 대상을
    같은 모양으로 맞추지 않으면 SQLAlchemy가 조건을 파이썬으로 평가하는 경로에서
    "can't compare offset-naive and offset-aware datetimes"로 죽는다.
    """
    if value.tzinfo is None:
        # 이미 naive라면 UTC로 간주한다. 이 모듈에 들어오는 값은 전부 UTC 계약이다.
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _as_utc_aware(value: Optional[datetime]) -> Optional[datetime]:
    """읽어 온 naive UTC 값에 tzinfo를 도로 붙인다 (응답에 오프셋을 실어 보내려고)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ScoreBucket:
    score: int
    count: int


@dataclass(frozen=True)
class FilteredSignalHistogram:
    """점수 구간별 건수 집계 결과.

    ``buckets``는 실제로 기록된 점수만 담는다 — 건수 0인 점수 칸을 지어내지 않는다.
    표시할 축이 필요한 쪽(사람이 읽는 표)에서 -3~+3을 채우는 편이, 여기서 채워
    "0건인 칸"과 "그 점수를 본 적 없음"을 뭉개는 것보다 낫다.
    """

    total: int
    buckets: tuple[ScoreBucket, ...]
    # 이 구간에 섞여 있는 기록 시점 임계값들. 값이 둘 이상이면 집계 구간 중간에
    # 임계값이 바뀌었다는 뜻이고, 그때는 구간 전체를 한 분포로 읽으면 안 된다.
    thresholds: tuple[int, ...]
    oldest: Optional[datetime]
    newest: Optional[datetime]


class SqliteFilteredSignalRepo:
    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    async def record(
        self,
        *,
        stock_name: str,
        source: str,
        score: Optional[int],
        threshold: int,
        reason: Optional[str] = None,
        uncertainty: Optional[float] = None,
    ) -> None:
        """걸러진 신호 한 건의 채점 결과를 남긴다. ``score``가 None이면 남기지 않는다.

        점수 없이 걸러지는 경로가 실제로 있다 — 본문이 비었거나 직전과 같은 signal은
        LLM을 부르지도 않고 걸러진다(services._SKIPPED_SIGNAL_SCORE). 그런 행을 남기면
        점수 분포에 보탤 정보는 0인데, 감시 루프가 종목·소스마다 10분 주기로 도는 탓에
        "이번 주기에도 새 소식이 없었다"는 행만 테이블을 가득 채운다. 그래서 여기 남는
        행은 전부 "모델이 실제로 채점했고 그 점수가 임계값에 못 미쳤다"는 뜻이다.
        """
        if score is None:
            return

        with self._session_factory() as session:
            row = FilteredSignal(
                stock_name=stock_name,
                source=source,
                score=score,
                threshold=threshold,
                reason=reason,
                uncertainty=uncertainty,
            )
            session.add(row)
            session.commit()

    async def purge_expired(
        self,
        retention_days: int,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """보존 기간이 지난 행을 지우고 삭제 건수를 돌려준다.

        행 단위로 세션에 실어 지우지 않고 한 문장으로 지운다 — 정리 대상은 수만 건일
        수 있고, 그걸 전부 파이썬 객체로 올릴 이유가 없다.

        워커가 여럿이어도 안전하다: 같은 조건으로 두 번 지우면 두 번째는 0건이다.
        """
        cutoff = _as_utc_naive(
            (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
        )
        with self._session_factory() as session:
            result = session.execute(
                delete(FilteredSignal).where(col(FilteredSignal.created_at) < cutoff),
                # 세션에 이미 올라온 객체를 파이썬으로 재평가하지 않는다. 이 세션은
                # 삭제 직후 버려지므로 동기화할 상태가 없고, 기본 전략("evaluate")은
                # 조건을 파이썬에서 다시 계산하다 타입 차이로 죽을 수 있다.
                execution_options={"synchronize_session": False},
            )
            session.commit()
            # DML의 실행 결과는 CursorResult이지만 Session.execute의 선언 반환형은
            # 그 상위인 Result라 rowcount가 보이지 않는다.
            # rowcount는 방언에 따라 -1("모름")일 수 있다. 로그 문구가 "-1건 삭제"가
            # 되지 않도록 0으로 눕힌다 — 삭제 자체는 이미 커밋됐다.
            return max(cast(CursorResult[Any], result).rowcount or 0, 0)


def score_histogram(
    session: Session,
    *,
    source: Optional[str] = None,
    stock_name: Optional[str] = None,
    since: Optional[datetime] = None,
) -> FilteredSignalHistogram:
    """걸러진 신호를 점수별로 세어 돌려준다.

    ``since``는 UTC 기준으로 넘긴다. created_at을 UTC로 저장하므로(models 참고)
    다른 시간대의 값을 그대로 넘기면 구간이 어긋난다.
    """
    # 조건도 정렬도 col()로 컬럼을 명시한다. 모델 필드를 그대로 쓰면 체커에는
    # 파이썬 값의 비교(bool)로 보여 SQL 표현식 자리에 들어가지 못한다.
    filters = []
    if source is not None:
        filters.append(col(FilteredSignal.source) == source)
    if stock_name is not None:
        filters.append(col(FilteredSignal.stock_name) == stock_name)
    if since is not None:
        filters.append(col(FilteredSignal.created_at) >= _as_utc_naive(since))

    bucket_query = select(FilteredSignal.score, func.count()).group_by(
        col(FilteredSignal.score)
    ).order_by(col(FilteredSignal.score))
    summary_query = select(
        func.count(),
        func.min(FilteredSignal.created_at),
        func.max(FilteredSignal.created_at),
    )
    threshold_query = select(FilteredSignal.threshold).distinct().order_by(
        col(FilteredSignal.threshold)
    )
    for condition in filters:
        bucket_query = bucket_query.where(condition)
        summary_query = summary_query.where(condition)
        threshold_query = threshold_query.where(condition)

    buckets = tuple(
        ScoreBucket(score=score, count=count)
        for score, count in session.exec(bucket_query).all()
    )
    total, oldest, newest = session.exec(summary_query).one()
    thresholds = tuple(session.exec(threshold_query).all())

    return FilteredSignalHistogram(
        total=total or 0,
        buckets=buckets,
        thresholds=thresholds,
        oldest=_as_utc_aware(oldest),
        newest=_as_utc_aware(newest),
    )


def fill_score_axis(
    buckets: Sequence[ScoreBucket], low: int, high: int
) -> tuple[ScoreBucket, ...]:
    """``low``~``high`` 전 구간을 0으로 메운 축을 만든다 (사람이 읽는 표·그래프용).

    집계 자체는 본 적 없는 점수를 지어내지 않는다. 축이 필요한 표시 계층만 이 함수를
    거친다.

    축 밖의 점수는 표시할 자리가 없어 버려지는데, 그러면 응답의 total과 buckets의 합이
    어긋난다. 지금은 채점이 -3~+3으로 clamp되어(services._coerce_signal_score) 도달할
    수 없는 상태지만, 그 clamp가 사라지면 무증상으로 깨지므로 버릴 때 로그를 남긴다.
    """
    out_of_axis = [bucket for bucket in buckets if not low <= bucket.score <= high]
    if out_of_axis:
        logger.warning(
            "점수 축(%d~%d) 밖의 집계 %d건을 표시에서 제외했습니다 — total과 buckets의 합이 "
            "어긋납니다: %s",
            low,
            high,
            len(out_of_axis),
            [(bucket.score, bucket.count) for bucket in out_of_axis],
        )
    counts = {bucket.score: bucket.count for bucket in buckets}
    return tuple(
        ScoreBucket(score=score, count=counts.get(score, 0))
        for score in range(low, high + 1)
    )
