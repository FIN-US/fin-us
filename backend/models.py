from datetime import date, datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel

class Portfolio(SQLModel, table=True):
    """
    사용자의 현재 주식 잔고 및 포트폴리오 정보를 저장합니다.
    증권사 API(SSOT)의 데이터를 기반으로 동기화됩니다.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    # unique=True가 아직 없다 (#196). scheduler._sync_portfolio_from_balance는
    # stock_code 기준 upsert이므로 논리적으로는 코드당 1행이 불변식이고, 그 함수가
    # 중복 행을 발견하면 한 행으로 수렴시킨다 — upsert의 정확성은 이 제약에
    # 의존하지 않는다.
    #
    # 제약을 켜려면 스키마 마이그레이션이 필요하다: 이미 배포된 SQLite 파일에는
    # 비유일 인덱스 ix_portfolio_stock_code가 이미 있고 create_all()은 기존
    # 인덱스를 바꾸지 않으므로, DROP INDEX → 중복 행 정리 → CREATE UNIQUE INDEX
    # 순서가 필요하다. database.py의 _PENDING_COLUMN_MIGRATIONS는 컬럼 추가
    # 전용이라 이 형태를 담지 못한다. 중복 행을 지우지 않은 채 유니크 인덱스를
    # 만들면 부팅이 영구 실패하므로, 별도 이슈에서 다룬다.
    stock_code: str = Field(index=True, description="종목 코드")
    stock_name: str = Field(description="종목명")
    quantity: int = Field(default=0, description="보유 수량")
    avg_price: float = Field(default=0.0, description="평균 매입가")
    # 이 값을 채우는 프로덕션 경로는 get_balance_rlz_pl(inquire-balance-rlz-pl,
    # TTTC8494R)의 텍스트 리포트를 파싱하는 scheduler._sync_portfolio_prices_from_rlz_pl
    # 하나뿐이다(#196). 잔고 동기화(_sync_portfolio_from_balance)는 이 필드를 읽지도
    # 쓰지도 않는다 — get_balance(TTTC8434R) output1에 현재가 필드가 있는지가
    # 미확인이라 그쪽에서 채울 근거가 없고, 덮으면 시세 경로가 방금 쓴 값이 10분마다
    # null로 지워진다(위 upsert 참고).
    current_price: Optional[float] = Field(default=None, description="현재가")
    # current_price를 마지막으로 갱신한 시각(UTC). updated_at과 **다른 축**이다:
    # updated_at은 "잔고를 마지막으로 확인한 시각"이라 시세 갱신 여부와 무관하게 매
    # 주기 갱신되므로, 그 값으로 시세 나이를 재면 항상 방금 갱신된 것처럼 보인다.
    #
    # null이면 "시세 나이를 모른다"이지 "시세가 없다"가 아니다. 이 컬럼이 없던 시절에
    # 저장된 행은 current_price가 채워져 있어도 언제 채워진 값인지 알 수 없어 백필하지
    # 않는다 — AgentReport.signal_score를 DEFAULT 없이 둔 것과 같은 기준이다(#122·#162).
    # scheduler.is_price_fresh가 이 null을 "모름"으로 판정해 price_known=False로 내린다.
    price_updated_at: Optional[datetime] = Field(
        default=None,
        description="current_price를 마지막으로 갱신한 시각 (UTC). null이면 시세 나이 미상 — 신선도 판정에서 '모름'.",
    )
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="최근 업데이트 시간")

class TradeHistory(SQLModel, table=True):
    """
    사용자의 주식 매매 내역을 저장합니다.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    stock_code: str = Field(index=True, description="종목 코드")
    stock_name: str = Field(description="종목명")
    trade_type: str = Field(description="매매 구분 (BUY/SELL)")
    quantity: int = Field(description="매매 수량")
    price: float = Field(description="매매 단가")
    trade_date: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="매매 일시")
    # 체결 통지 outbox의 상태 컬럼 (#259 2단계). null이면 "이 체결에 대한 통지가 아직
    # 나가지 않았다"는 뜻이고, scheduler.trade_notification_task가 재배달 대상으로 집는다.
    #
    # 별도 통지 테이블 대신 이 컬럼 하나로 끝내는 이유: 체결 사실은 이미 이 행으로
    # 영속화돼 있어 "한 체결 = 한 통지"가 자연히 성립하고, 멱등 키도 이 행의 PK를 그대로
    # 쓸 수 있다. 통지 원장을 따로 두면 체결 원장과 갈려 정합을 맞출 지점이 새로 생긴다.
    #
    # trade_date와 같은 축(tz 없는 UTC)으로 저장된다 — order_assist._kst_day_start_utc의
    # 설명 참조. 두 값을 빼서 지연을 재는 코드가 생길 수 있으므로 축이 갈리면 안 된다.
    notified_at: Optional[datetime] = Field(
        default=None,
        description="체결 통지 전송 완료 시각 (UTC). null이면 미통지 — outbox 재배달 대상",
    )

class AgentReport(SQLModel, table=True):
    """
    AI 에이전트가 생성한 종목 분석 리포트 및 투자 제안을 저장합니다.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    stock_code: str = Field(index=True, description="분석 종목 코드")
    stock_name: str = Field(index=True, description="분석 종목명")
    provider: str = Field(description="사용된 LLM 제공자 (openai, anthropic 등)")
    summary: str = Field(description="분석 요약")
    # A (#162): provider_supports_tools=False인 provider(openai/anthropic/ollama)는
    # 도구 없이 매매 판단을 생성하지 않는다. 이 경우 두 필드는 null이다.
    # null과 "HOLD"/0.0의 의미가 다르다 — null은 "판단 없음"이고
    # "HOLD"/0.0은 NAT이 판단한 관망 의견이다.
    decision: Optional[str] = Field(
        default=None,
        description=(
            "투자 결정 (BUY/SELL/HOLD). provider_supports_tools=True인 provider(nat)만 생성한다. "
            "도구 없는 provider(openai/anthropic/ollama)는 null."
        ),
    )
    confidence_score: Optional[float] = Field(
        default=None,
        description=(
            "신뢰도 점수 (0.0~1.0). provider_supports_tools=True인 provider(nat)만 생성한다. "
            "도구 없는 provider(openai/anthropic/ollama)는 null."
        ),
    )
    reason: str = Field(default="", description="투자 결정 근거. 도구 없는 provider는 빈 문자열.")
    # provider 차원의 "능력" 신호 — 실제 도구 호출 관측이 아님 (#162):
    # - openai/anthropic/ollama: tools 파라미터 없이 모델 직접 호출 → False는 코드로 증명 가능
    # - nat=True: NAT 멀티에이전트로 라우팅됐다는 뜻이며, 그 안에서 실제 도구가
    #   실행됐는지는 이 필드가 보장하지 않는다 (NAT ReAct가 도구 없이 답할 수 있는
    #   문제 #152, raise_on_parsing_failure: false). false negative는 구조적으로
    #   불가능하지만 false positive는 가능한 비대칭 신호다.
    # - 실제 도구 호출 이력(ledger)은 #152의 몫이며 이 필드는 대신하지 않는다.
    # - 이 컬럼이 없던 구버전 행은 database._run_schema_migrations()가
    #   ALTER TABLE ... DEFAULT 0으로 채운다 — False는 "도구 미지원"과
    #   "과거 행이라 확인 불가" 둘 다를 의미하며, 절대 True로 소급되지 않는다.
    # - services.provider_supports_tools()가 provider 자체에서 파생하며,
    #   호출부가 직접 True/False를 넘기지 않는다.
    provider_supports_tools: bool = Field(
        default=False,
        description="provider가 도구(MCP/KIS/뉴스) 호출 경로로 구성돼 있는지 여부 (provider 능력 신호, 실제 호출 관측 아님).",
    )
    # #298: 2차 필터가 매긴 신호 점수. 셋 다 nullable이며 기본값을 채우지 않는다 —
    # "0과 모름"은 다른 값이다(#122·#162). null은 "채점 자체가 없었다"는 뜻이고,
    # LLM 호출·파싱 실패로 fail-open했거나, 점수화 이전에 저장된 행이거나,
    # 스케줄러를 거치지 않은 수동 분석인 경우다.
    #
    # 이 테이블에 실제로 남는 점수는 |score| >= SIGNAL_SCORE_THRESHOLD인 값뿐이다.
    # 임계값 미만이면 스케줄러가 상세 분석 자체를 건너뛰므로 리포트 행이 생기지
    # 않는다 — 즉 기본 임계값에서 0이나 ±1인 행은 구조적으로 존재할 수 없다.
    # 걸러진 신호의 점수 분포는 scheduler의 "유의미한 변화 없음(score=...)" 로그와
    # 평가셋(backend/scripts/build_signal_eval_set.py)에서 본다.
    signal_score: Optional[int] = Field(
        default=None,
        description=(
            "신호 영향도 점수 (-3~+3 정수). 감시 파이프라인의 2차 필터가 매긴다. "
            "채점하지 못했으면 null (fail-open)."
        ),
    )
    signal_reason: Optional[str] = Field(
        default=None,
        description="signal_score의 한 줄 근거. 점수가 null이면 함께 null.",
    )
    signal_uncertainty: Optional[float] = Field(
        default=None,
        description=(
            "기사별 점수의 표준편차. 기사가 2건 미만이면 흩어짐을 정의할 수 없으므로 null "
            "(0.0이 아니다 — 0.0은 '기사들이 완전히 일치했다'는 뜻)."
        ),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="생성 일시")

class Diary(SQLModel, table=True):
    """
    사용자의 개인 투자 일지 및 메모를 저장합니다.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(description="일지 제목")
    content: str = Field(description="일지 내용")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="작성 일시")


class WatchlistItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    stock_name: str = Field(unique=True, index=True)


class CatalystEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    stock_name: str = Field(index=True)
    stock_code: Optional[str] = Field(default=None, index=True)
    event_type: str = Field(index=True, description="earnings | dividend | disclosure | agm")
    event_date: date = Field(index=True)
    description: str
    source: str = Field(default="manual")
    notified: bool = Field(default=False, index=True)
    d1_notified: bool = Field(default=False)
    d0_notified: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="생성 일시",
    )


class FilteredSignal(SQLModel, table=True):
    """임계값 미만이라 상세 분석까지 가지 않은 신호의 채점 결과 (#304).

    AgentReport에는 |score| >= SIGNAL_SCORE_THRESHOLD인 신호만 남는다 — 미만이면
    스케줄러가 상세 분석 자체를 건너뛰므로 리포트 행이 생기지 않는다. 임계값을
    조정하려면 "지금 값에서 걸러지고 있는 쪽"의 분포가 필요한데, 그 데이터는
    scheduler의 "유의미한 변화 없음(score=...)" 로그에만 있어 grep은 되지만 집계가
    안 됐다. 이 테이블이 그 분포를 구조화해 남긴다.

    AgentReport에 "분석 미실행" 행을 섞지 않고 별도 테이블로 둔 이유: 저쪽은
    "행이 있다 = 상세 분석을 돌렸다"가 불변식이다. 분석 없는 행을 섞으면 그 테이블을
    읽는 모든 경로(/api/v1/db/reports, 프론트, 텔레그램 트렌드 집계)가 그것을 걸러
    내도록 바뀌어야 하고, 한 곳만 빠뜨려도 summary·decision이 빈 행이 리포트로
    노출된다. 스키마도 갈라진다 — 저쪽의 provider/summary/reason은 여기서 채울 값이
    없어 전부 빈 문자열이 된다.

    signal 본문 원문은 남기지 않는다 (#304의 명시적 결정). 보존 비용과 개인정보 범위가
    커지는 데 비해, 이 테이블의 목적인 점수 분포 집계에는 필요하지 않다. 본문이 필요한
    작업(프롬프트 조정)은 평가셋(backend/scripts/build_signal_eval_set.py)의 몫이다.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    # 감시 루프는 종목 코드를 모른 채 종목명으로 돈다. 코드를 채우려면 종목당 조회가
    # 한 번씩 더 붙는데, 걸러진 신호는 알림도 리포트도 만들지 않는 값이라 그 비용을
    # 낼 이유가 없다.
    stock_name: str = Field(index=True, description="감시 대상 종목명")
    source: str = Field(index=True, description="신호 출처 (news | disclosure)")
    # AgentReport.signal_score와 달리 NOT NULL이다. 저쪽의 null은 "채점하지 못했다"
    # (fail-open)인데, fail-open한 신호는 유의미로 통과해 상세 분석까지 가므로 애초에
    # 이 테이블에 오지 않는다. 즉 여기 남는 행은 전부 "모델이 실제로 매긴 점수"이며,
    # 그래서 집계에 null 분기가 필요 없다.
    score: int = Field(index=True, description="신호 영향도 점수 (-3~+3 정수)")
    # 기록 시점에 적용된 임계값. 이 값을 남기지 않으면 나중에 임계값을 바꾼 뒤
    # 과거 행이 "왜 걸러졌는지"를 설명할 수 없다 — 같은 1점이 임계값 2에서는 걸러진
    # 신호이고 1에서는 통과한 신호다.
    threshold: int = Field(description="기록 시점의 SIGNAL_SCORE_THRESHOLD")
    reason: Optional[str] = Field(
        default=None, description="점수 근거 한 줄. 모델이 근거를 주지 않았으면 null."
    )
    uncertainty: Optional[float] = Field(
        default=None,
        description=(
            "기사별 점수의 표준편차. 기사가 2건 미만이면 흩어짐을 정의할 수 없으므로 null "
            "(0.0이 아니다 — 0.0은 '기사들이 완전히 일치했다'는 뜻)."
        ),
    )
    # 보존 기간 정리(FILTERED_SIGNAL_RETENTION_DAYS)와 구간 집계가 모두 이 컬럼을
    # 기준으로 도므로 인덱스를 건다. UTC로 저장한다.
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        index=True,
        description="기록 일시 (UTC)",
    )
