"""텔레그램 getUpdates 폴링 루프(업데이트 재시도·건너뜀)와 봇 시작·종료."""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from backend.core.redis_state import TelegramPollerFailure, TelegramPollerState, TelegramPollerStore
from backend.telegram.commands import TelegramCommandHandler
from backend.telegram.handlers import base as handler_base
from backend.telegram.handlers.base import (
    _create_order_gateway,
    _create_pending_order_store,
    _create_poller_state_store,
    _update_settled_hook,
    TelegramSendError,
)
from backend.telegram.handlers.texts import TELEGRAM_BOT_COMMANDS
from backend.telegram.notifier import fetch_telegram_api, telegram_notifier, TelegramPollerNotifier

logger = logging.getLogger(__name__)


# 실패한 update는 offset을 유지해 재시도한다(일시 장애 때 명령을 버리지 않기 위함).
# 다만 결정론적으로 실패하는 update는 영원히 재시도되며 뒤의 명령까지 막으므로
# 예산을 넘기면 건너뛴다 (#241).
#
# 예산은 "횟수"가 아니라 "시간"이다. 고정 5초 × 3회는 실질 10초여서 redis 재시작·컨테이너
# 재기동·배포 어느 것도 버티지 못한다 — 일시 장애에 명령을 버리지 않겠다는 원래 목적을
# 달성하지 못한다 (PR #242 리뷰).
UPDATE_RETRY_WINDOW_SECONDS = 60.0
# 전송 실패는 그 update의 문제가 아니라 전역 장애(429 rate limit·5xx)다. 같은 예산을 쓰면
# 429 몇 초 때문에 대기 중인 명령이 전부 폐기되는데, 폐기 통지도 같은 이유로 실패해
# 사용자는 아무것도 못 받는다. 기존 동작(막히더라도 복구되면 전부 실행) 대비 회귀이므로
# 전송 실패에는 훨씬 긴 창을 준다 (PR #242 리뷰).
#
# 이 값은 무한정 키우면 안 된다. handle_update가 던지는 지배적 경로가 전송 실패라서,
# 영구적 전송 실패(특정 메시지의 Markdown 파싱 400)가 하나 들어오면 offset이 이 시간만큼
# 얼어붙는다. #259 1단계 이전에는 그 동결이 "#241의 재발"이었지만 지금은 채택한 설계다 —
# 배치가 그 update에서 끊기는 것이 이 봇이 중복 실행 대신 고른 쪽이다. 그래서 상한이 필요한
# 이유는 오히려 더 강해졌다: 얼어붙는 동안 뒤의 명령도 실행되지 않아 봇이 이 시간만큼 통째로
# 무응답이다. 상한이 있다는 사실 자체를 테스트가 고정한다
# (test_poller_eventually_skips_persistent_send_failures).
SEND_FAILURE_RETRY_WINDOW_SECONDS = 300.0
# 재시도 간격. 실패가 이어지면 폴링을 늦춰 텔레그램 rate limit을 자극하지 않는다.
# 폴러는 단일 인스턴스라 동시 기상이 겹칠 일이 없어 jitter는 두지 않았다.
#
# 위 두 창과 맞물려 실제 예산은 이렇게 정해진다 (PR #242 리뷰):
#   일반 실패   t=0, 5, 20, 65  → 4시도 / 65초 후 폐기
#   전송 실패   t=0, 5, 20, 65, 110 … 335 → 10시도 / 335초 후 폐기 (마지막 간격 45초 반복)
UPDATE_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 45.0)
UPDATE_SKIPPED_NOTICE = "요청 처리에 실패했어요. 다시 시도해주세요."
# 통지에 붙일 실패 명령 요약의 최대 길이. 명령을 연달아 보낸 사용자가 무엇을 다시
# 보내야 하는지 알 수 있어야 한다 (PR #242 리뷰).
UPDATE_LABEL_LIMIT = 40
# 폴러 상태 저장소 호출의 상한. 이게 없으면 fail-open이 fail-hang으로 무너진다: 저장소가
# 예외 대신 hang하면 _persist_state의 await가 돌아오지 않고, 그 await는 run()의 배치 루프
# 안이라 폴러 태스크가 통째로 멈춘다. except Exception은 예외가 나야 도는데 hang은 예외를
# 내지 않으므로 "인메모리로 계속" 로그조차 남지 않는다.
#
# #268이 create_redis_client()에 socket_timeout을 걸어 redis 클라이언트발 hang은 소켓에서
# 먼저 잘리지만, 이 wait_for는 그대로 남긴다. 두 상한이 덮는 범위가 다르기 때문이다:
# state_store는 덕 타이핑된 주입 지점이라 redis가 아닌 구현이 들어올 수 있고(테스트의
# 인메모리 저장소가 그렇다), 소켓 타임아웃은 명령 하나의 상한이지 호출 하나의 상한이 아니다
# — load()나 save()가 명령을 여럿 쓰게 되면 상한이 그만큼 곱해진다.
#
# 두 값이 같아(둘 다 3.0) redis 저장소에서는 어느 쪽이 먼저 발화할지 정해져 있지 않다.
# 결과는 같고(삼킨 뒤 인메모리로 계속) 로그 문구만 갈린다 — 소켓 쪽이 이기면 "Timeout
# reading from host:port"가 붙어 원인이 더 분명해진다.
#
# 이 PR이 노출을 넓혔다는 점이 근거다: 이전에는 redis를 만지는 명령(/alerts, /buy, /confirm,
# /cancel)만 이 위험을 졌고 /help·자연어·/watch는 blackhole 중에도 서비스됐지만, 이제 모든
# update가 루프 안에서 redis를 동기 대기한다 (PR #251 리뷰).
#
# 값은 사용자 체감(폴링 지연)과 일시적 지연 흡수 사이의 절충이다. 정상 redis는 1ms 수준이라
# 이 상한에 닿는 것은 이미 비정상이며, docker-compose에서 가장 흔한 장애(컨테이너 다운)는
# 즉시 ECONNREFUSED를 내므로 이 경로를 타지 않는다.
#
# 값을 조정할 때 알아둘 성질: 타임아웃은 update마다 걸리므로 hang 중 배치 하나가 멈추는
# 시간은 "배치 크기 × 이 값"으로 선형이다(응답 자체는 handle_update가 먼저 끝내 나간 뒤,
# 그 뒤에 죽은 시간이 붙는다). getUpdates limit이 100이면 최악 약 300초다 (PR #251 리뷰).
STATE_STORE_TIMEOUT_SECONDS = 3.0


# 확정 전송의 재시도 상수(SETTLED_SEND_*)와 루프(send_text_settled)는 telegram_notifier로
# 옮겼다 (PR #327 리뷰). 스케줄러의 자동 제안(#314)이 같은 재시도를 써야 하는데, 핸들러
# 메서드로 두면 그쪽에서 부를 수 없어 한쪽만 단발 전송이 되고 같은 429에 한쪽 메시지만
# 조용히 버려진다. 이름은 위 import로 여기서 계속 읽을 수 있다.
#
# 다만 **여기 있는 이름을 monkeypatch해도 상한은 바뀌지 않는다** — 값을 읽는 것은
# telegram_notifier 안이고 이쪽은 임포트 시점에 묶인 별개 바인딩이다. 테스트가 상한을
# 줄이려면 telegram_notifier 쪽을 패치해야 하며, 잘못된 대상을 잡으면
# test_settled_send_gives_up_at_the_wall_clock_bound의 elapsed 단언이 잡는다.

# getUpdates 배치 크기. 명시하지 않으면 Telegram 기본값이 100이라, 배치 전체가 settled
# 전송에 닿으면 한 루프의 점유가 100 × SETTLED_SEND_TIMEOUT_SECONDS까지 늘어나 사실상
# 상한이 없어진다. 배치를 작게 끊으면 최악이 유계가 된다 (PR #253 2차 리뷰).
#
# #259 2단계가 이 계산에서 가장 큰 항을 뺐다 — 체결 통지가 outbox로 나가면서 /confirm
# 성공 경로는 더 이상 settled 전송을 쓰지 않는다. 남은 settled 경로(/buy 프롬프트,
# /cancel, /confirm 403·불명확, /earnings·자연어)는 그대로라 이 상한은 계속 필요하다.
GET_UPDATES_LIMIT = 10

# 각주 상수(REASONING_FOOTNOTE_*)와 라벨 표(AGENT_LABELS·TOOL_LABELS), 각주 조립 자체는
# #297에서 presentation으로 옮겼다. 동작은 그대로다 — 옮긴 이유는 나가는 문장을 조립하는
# 지점이 하나여야 용어 각주와의 순서·길이 예산을 한 곳에서 정할 수 있기 때문이다.
# 이 모듈은 그 이름들을 다시 내보내지 않는다. 재수출만 남은 임포트는 정의가 두 곳에 있다는
# 착시를 만들고, 정적 검사에는 미사용 임포트로 보인다 (#297 자가리뷰).

_telegram_command_task: asyncio.Task | None = None


def _update_sort_key(update: dict[str, Any]) -> tuple[int, int]:
    """update_id 오름차순 정렬 키. update_id가 없는 update는 맨 앞으로 보낸다 (#241).

    #259 1단계로 "poison 뒤 update 선행 실행"은 되돌렸지만 이 정렬은 남는다. 배치를 poison에서
    끊는 것은 미해결 update를 그보다 뒤의 update보다 먼저 만난다는 전제 위에서만 offset을
    지켜주기 때문이다 — 역순 배치라면 뒤쪽 update가 먼저 성공해 offset이 poison을 지나가고,
    그러면 그 poison은 재배달 대상에서 조용히 사라진다.

    update_id가 없는 update는 재시도 예산도 offset도 추적할 수 없어 실패해도 그 자리에서
    끝난다(= 배치를 끊지 않는다). 그래서 앞에 두어도 뒤에 오는 update의 재시도 판정에
    영향을 주지 않는다. 다만 offset이 그걸 지나갈 수 없어 재배달될 때마다 다시 실행되고,
    배치가 끊기지 않아 sleep도 없으므로 getUpdates busy loop가 된다. 실제 Telegram은 항상
    update_id를 주므로 이론적 경로다 (PR #242 리뷰).
    """
    update_id = update.get("update_id")
    if isinstance(update_id, int):
        return (1, update_id)
    return (0, 0)


class _UpdateOutcome(Enum):
    """_handle_one_update의 결과 (#241).

    offset을 전진시켜도 되는지(DONE/SKIPPED)와 스킵 통지가 필요한지(SKIPPED)를 한 값으로
    구분한다. 평문 str이면 오타가 조용히 통과하므로 Enum으로 강제한다 (PR #242 리뷰).

    COMMITTED는 handle_update가 처리 도중에 이미 offset을 확정·영속화했다는 뜻이다
    (_update_settled_hook, #259 3단계). 전진시켜도 되는 것은 DONE과 같지만, 배치 루프가 같은
    쓰기를 반복하지 않도록 구분한다.
    """

    DONE = "done"
    RETRY = "retry"
    SKIPPED = "skipped"
    COMMITTED = "committed"


def _update_chat_id(update: dict[str, Any]) -> str:
    callback_query = update.get("callback_query") or {}
    message = update.get("message") or callback_query.get("message") or {}
    return str((message.get("chat") or {}).get("id", "")).strip()


def _update_label(update: dict[str, Any]) -> str:
    """실패 통지에 붙일 요약. 사용자가 무엇을 다시 보내야 하는지 알 수 있게 한다 (#241)."""
    callback_query = update.get("callback_query") or {}
    message = update.get("message") or callback_query.get("message") or {}
    text = (message.get("text") or "").strip()
    if not text:
        text = str(callback_query.get("data") or "").strip()
    if not text:
        return f"update {update.get('update_id')}"
    if len(text) > UPDATE_LABEL_LIMIT:
        return text[:UPDATE_LABEL_LIMIT] + "…"
    return text


@dataclass
class _UpdateFailure:
    """update별 재시도 예산. 기준은 횟수가 아니라 첫 실패 이후 흐른 시간이다 (#241).

    시계가 둘인 이유는 예산이 재시작을 넘겨야 하기 때문이다 (#350). ``first_at``은
    monotonic이라 경과 계산이 벽시계 조정에 흔들리지 않지만 프로세스가 죽으면 원점이
    사라지고, ``first_wall_at``은 그 반대다. 그래서 진행 중인 판정은 monotonic으로 하고
    영속화·복원 경계에서만 벽시계를 쓴다 — 복원은 벽시계 경과만큼 뒤로 민 monotonic 값을
    ``first_at``에 넣어, 그 뒤의 코드가 계속 한 시계만 보게 한다.

    ``first_wall_at``은 복원해도 원래 값을 그대로 유지한다 — 앵커가 미래일 때(벽시계가 뒤로
    조정된 경우)만 예외이고, 근거는 _restore_failures에 있다. 그 예외 밖에서 복원 시점으로
    새로 찍으면 재시작마다 앵커가 앞당겨져, 잦은 재시작이 다시 예산을 무한히 늘리는 #350의
    경로가 벽시계 쪽으로 되살아난다.
    """

    first_at: float
    attempts: int
    send_failure: bool
    first_wall_at: float


class TelegramCommandPoller:
    def __init__(
        self,
        *,
        notifier: TelegramPollerNotifier = telegram_notifier,
        handler: TelegramCommandHandler | None = None,
        state_store: TelegramPollerStore | None = None,
    ):
        self.notifier = notifier
        if handler is None:
            handler = TelegramCommandHandler(
                notifier=notifier,
                order_gateway=_create_order_gateway(),
                trade_recorder=handler_base._create_trade_recorder(),
                pending_order_store=_create_pending_order_store(),
            )
        self.handler = handler
        # offset은 redis에 영속화된다 (#248). 인메모리였을 때는 재시작이 offset을 처음으로
        # 되감아, 배치 중간까지 실행한 update가 통째로 재배달·재실행됐다.
        #
        # #248이 offset과 함께 저장하던 _handled_ahead는 #259 1단계에서 사라졌다. 그 집합은
        # "poison 뒤 update를 먼저 실행한다"는 #241의 최적화가 만들어낸 중복 실행 위험을 막는
        # 장치였는데, 최적화 자체를 되돌려 poison에서 배치를 끊게 하면서 기억해야 할 선행
        # 실행이 없어졌다. 남는 창은 handle_update 한 번의 실행 시간뿐이라 offset만 저장한다
        # (#270 → #259).
        self.state_store: TelegramPollerStore = (
            state_store if state_store is not None else _create_poller_state_store()
        )
        self.offset: int | None = None
        # update_id -> 재시도 예산. offset과 함께 영속화된다 (#350).
        #
        # #248은 이 값을 일부러 뺐다. 근거는 "유실돼도 poison 폐기가 미뤄질 뿐 중복 실행으로
        # 이어지지 않는다"였는데, #259 1단계가 배치를 poison에서 끊게 만들면서 그 대가가
        # **그동안 그 채팅의 모든 명령이 정지**로 커졌다. #241 하에서는 뒤의 update가 선행
        # 실행돼 "폐기가 미뤄짐"에 그쳤다. 리셋 계기는 배포가 아니라 Dockerfile의
        # uvicorn --reload + bind mount라 backend 파일 저장 하나면 되고, 예산(일반 65초·
        # 전송 실패 335초)보다 잦은 재시작이 이어지면 poison은 영원히 폐기되지 않았다.
        # (#396에서 기본 CMD의 --reload는 뺐다. override 예시로 리로드를 켠 개발 구성과
        # 잦은 수동 재시작에는 같은 논리가 그대로 걸린다.)
        #
        # 방향이 offset과 반대라는 점은 그대로다: offset은 "자신을 poison에 붙들어 매는
        # 상태"이고 이건 "poison을 포기하게 해주는 유일한 상태"다 (PR #251 리뷰). 그래서
        # 유실 시의 degrade 방향도 반대다 — offset이 없으면 재실행, 이게 없으면 정지.
        # 복원 실패를 저장소가 offset과 다르게 다루는 근거가 그것이다
        # (RedisTelegramPollerStore._deserialize_failures).
        self._failures: dict[int, _UpdateFailure] = {}
        # 복원이 저장소를 실제로 읽어냈는가 (PR #362 리뷰). 거짓이면 저장된 offset이
        # 무엇인지 이 프로세스는 모른다 — self.offset이 None인 것은 "없다"가 아니라
        # "모른다"다. 그 구분이 필요한 곳은 아래 retry_pending의 쓰기 하나뿐이다.
        self._state_restored = False

    async def run(self) -> None:
        if not self.notifier.enabled:
            return
        await self._setup_bot_profile()
        await self._restore_state()

        while True:
            try:
                updates = await self._get_updates()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # getUpdates 자체의 네트워크 오류는 기존대로 5초 후 재시도한다.
                logger.error("Telegram command polling failed: %s", exc)
                await self._sleep(UPDATE_RETRY_BACKOFF_SECONDS[0])
                continue

            # 재시도 대기 중인 update를 만나면 배치를 그 자리에서 끊는다. #241은 반대로
            # 뒤의 update를 계속 실행해 head-of-line blocking을 없앴는데, 그 선행 실행분은
            # offset이 확정하지 못한 채 재배달되므로 "무엇을 이미 실행했는지"를 기억하는
            # 상태(_handled_ahead)가 딸려왔다. 그 상태의 유실은 곧 주문 부수효과의 중복
            # 실행이고, 그것을 막을 장치는 상태 자신뿐이었다. 단일 채팅·단일 프로세스인 이
            # 봇에서 선행 실행이 사는 값은 "내 다음 명령이 최대 335초(전송 실패 예산) 늦게
            # 실행되는 것"을 피하는 정도라, 중복 실행 위험과 맞바꾸지 않는다 (#270 → #259).
            #
            # 끊는 것은 미해결 update를 그보다 뒤의 update보다 먼저 만난다는 전제 위에서만
            # offset을 지켜준다. getUpdates가 오름차순을 보장한다는 외부 전제에 정확성을
            # 걸지 않도록 여기서 직접 정렬해 전제를 없앤다 (#241).
            updates = sorted(updates, key=_update_sort_key)
            retry_pending = False
            skipped: list[dict[str, Any]] = []
            for update in updates:
                update_id = update.get("update_id")
                outcome = await self._handle_one_update(update, update_id)
                if outcome is _UpdateOutcome.RETRY:
                    retry_pending = True
                    break
                if outcome is _UpdateOutcome.SKIPPED:
                    skipped.append(update)
                if outcome is _UpdateOutcome.COMMITTED:
                    # 핸들러가 처리 도중에 이미 확정했다 (#259 3단계). 같은 쓰기를 반복하지 않는다.
                    continue
                if not isinstance(update_id, int):
                    continue

                await self._commit_update(update_id)

            if skipped:
                # 배치 통지는 한 건으로 합친다. poison N건에 N번 발송하면 채팅당 초당 ~1건
                # 제한에 걸려 429를 만들고, 429는 다시 전송 실패 = 새 poison이 된다 (PR #242 리뷰).
                #
                # #259 1단계 이후 이 목록의 원소는 실제로는 최대 1개다 — 예산은 시도해야 쌓이고
                # 시도는 첫 RETRY에서 끊기므로, 한 배치에서 예산을 소진할 수 있는 update는
                # 맨 앞의 것 하나뿐이다. 합치는 코드를 남겨두는 이유는 그 상한이 배치 루프의
                # 모양에 딸린 성질이라서다: 루프가 다시 바뀌면 N건이 되살아나는데, 그때
                # 통지가 429를 만드는 경로는 여기서만 막힌다. 상한 자체는
                # test_skip_notice_merges_updates_into_one_message가 helper 단위로 고정한다.
                await self._notify_updates_skipped(skipped)
            if retry_pending:
                # 예산을 여기서 쓴다 (#350). 위 루프의 쓰기 지점은 update를 통과시킨 뒤에만
                # 도는데, RETRY는 배치를 그 자리에서 끊어 거기 닿지 못한다 — 이 한 줄이
                # 없으면 예산은 영속화 대상이 되고도 실제로는 한 번도 저장되지 않는다.
                #
                # 이 쓰기는 재시도 사이클당 한 번(간격 5~45초)이라 정상 경로의 쓰기 빈도를
                # 바꾸지 않는다. offset은 그대로고 바뀌는 것은 attempts뿐이지만, 상태를 통째로
                # 덮어쓰는 계약이라 부분 갱신을 따로 만들 이유가 없다.
                #
                # 다만 그 "통째로"가 여기서는 위험하다 (PR #362 리뷰). 복원이 실패했고 아직
                # offset을 하나도 확정하지 못했다면, self.offset(None)을 쓰는 순간 저장된
                # 정상 offset이 지워진다. 루프 안의 쓰기 지점은 offset을 전진시킨 직후라 이
                # 상태로 도달하지 않지만 여기는 도달한다 — poison이 배치 맨 앞이면 "첫 성공한
                # update"가 영영 오지 않아, 재시도 사이클마다 같은 쓰기가 반복된다. 그 offset은
                # 재배달·재실행을 막는 마지막 방어선이므로 예산 한 창과 바꾸지 않는다
                # (_deserialize_failures 독스트링의 판단과 같다). 예산은 그동안 인메모리로만
                # 살고, offset을 하나라도 확정하는 순간부터 정상적으로 저장된다.
                if self._state_restored or self.offset is not None:
                    await self._persist_state()
                # 실패한 update를 곧바로 다시 받아 예산을 순식간에 소진하지 않도록 쉬어 간다.
                # 실패가 이어지면 간격을 늘려 복구 창을 벌고 rate limit도 덜 자극한다 (#241).
                await self._sleep(self._retry_delay())

    async def _handle_one_update(
        self,
        update: dict[str, Any],
        update_id: Any,
    ) -> _UpdateOutcome:
        """update 처리 결과를 반환한다: 완료 / 재시도 대기 / 예산 소진 후 스킵 / 처리 도중 확정
        (#241, #259 3단계).

        재시도는 handle_update가 멱등하다는 전제 위에 있고, 그 전제는 핸들러가 지킨다:
        부수효과가 확정된 뒤의 전송은 예외를 던지지 않으므로 여기까지 오지 않는다. 즉
        재실행되는 것은 부수효과 이전 구간뿐이다 (#247). /confirm 체결 성공은 이 전제에 기대지
        않는다 — 전송 전에 _update_settled_hook으로 확정하므로, 그 뒤의 예외는 아래 committed
        분기가 COMMITTED로 받아 재시도하지 않는다. 그 전송이 실패했을 때 무엇으로
        되살리는지는 경로마다 다르다 — /confirm 체결 성공은 원장에 남은 미통지 행과
        scheduler.trade_notification.trade_notification_task가 받고(#259 2단계), 나머지 settled 경로는
        _send_text_settled의 인플레이스 재시도가 전부다. 그 전송 도중 재시작하면 update가
        재배달·재실행되는데, 이 창은 수용했다(#383 — _commit_update 참조).

        반대로 부수효과 이전의 전송 실패는 변환 경로(except Exception)에 삼켜지지 않고
        반드시 여기 도달한다 — 전송을 본문에 둔 try는 TelegramSendError를 재던져야 하고,
        test_every_try_containing_a_retryable_send_reraises_it이 그것을 정적으로 강제한다
        (#249). 그 가드는 직접 호출만 보므로, 전송을 감싼 헬퍼를 try 안에서 부르는 코드가
        생기면 이 전제가 조용히 깨진다.

        비싼 호출을 태우는 경로 — 자연어 폴백(_handle_chat_fallback)·/advise·/earnings — 는
        그 호출 뒤의 전송이 답변이든 실패 통지든 _send_text_settled라 여기 도달하지 않는다.
        재시도되면 LLM·제안 에이전트·DART 조회가 되풀이되고, 자연어·/advise는 진행
        메시지(#260)까지 매번 새로 나가며 그 전송 실패는 notifier가 삼켜 예산에도 잡히지
        않는다. 실패 통지는 예전에 _send_text_or_raise였고 #259 4단계에서 settled로 옮겼다
        (PR #263 리뷰, #275, PR #377 리뷰). /balance·/quote·/trend처럼 MCP 조회 한 번 뒤의
        실패 통지는 재시도해도 조회 한 번이 되풀이될 뿐이라 재시도 가능한 전송으로 남긴다.
        """
        committed = False

        async def commit() -> None:
            nonlocal committed
            # update_id가 없으면 확정할 offset이 없다. 한 update 안에서 두 번 불려도 한 번만 쓴다.
            if committed or not isinstance(update_id, int):
                return
            committed = True
            await self._commit_update(update_id)

        hook_token = _update_settled_hook.set(commit)
        try:
            await self.handler.handle_update(update)
            return _UpdateOutcome.COMMITTED if committed else _UpdateOutcome.DONE
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if committed:
                # offset이 이미 이 update를 지나갔다 (#259 3단계). 여기서 RETRY를 돌려주면
                # 재배달되지 않을 update에 예산이 생기고 배치만 끊긴다. 확정은 "다시 실행하면
                # 안 된다"는 핸들러의 선언이므로 뒤의 예외도 재시도로 되살리지 않는다.
                logger.error(
                    "Telegram update %s failed after it was settled: %s", update_id, exc
                )
                return _UpdateOutcome.COMMITTED
            if not isinstance(update_id, int):
                # update_id가 없으면 예산도 offset도 추적할 수 없다. 붙잡아 둘 방법이
                # 없으므로 로그만 남기고 넘어간다.
                logger.error("Telegram update handling failed without update_id: %s", exc)
                return _UpdateOutcome.DONE

            send_failure = isinstance(exc, TelegramSendError)
            now = self._now()
            failure = self._failures.get(update_id)
            if failure is None:
                failure = _UpdateFailure(
                    first_at=now,
                    attempts=1,
                    send_failure=send_failure,
                    first_wall_at=self._wall_now(),
                )
                self._failures[update_id] = failure
            else:
                failure.attempts += 1
                # 한 번이라도 전송 실패가 있었으면 긴 창을 유지한다. first_at은 첫 실패에
                # 고정인데 창을 마지막 예외 종류로 매번 다시 고르면 두 값의 기준이 어긋난다:
                # 429가 290초 이어지다 마지막에 redis 오류가 한 번 나면 창이 60초로 줄어
                # 그 오류에 재시도가 0회 주어진 채 즉시 폐기된다. 종류가 바뀔 때마다
                # first_at을 리셋하면 종류를 번갈아 던지는 update가 영원히 살아남으므로,
                # 상한이 유계인 이 방식을 택했다 (PR #242 리뷰).
                failure.send_failure = failure.send_failure or send_failure

            elapsed = now - failure.first_at
            window = (
                SEND_FAILURE_RETRY_WINDOW_SECONDS
                if failure.send_failure
                else UPDATE_RETRY_WINDOW_SECONDS
            )
            if elapsed <= window:
                logger.error(
                    "Telegram update %s handling failed (attempt %s, %.0fs/%.0fs): %s",
                    update_id,
                    failure.attempts,
                    elapsed,
                    window,
                    exc,
                )
                return _UpdateOutcome.RETRY

            logger.error(
                "Telegram update %s skipped after %s attempts over %.0fs: %s",
                update_id,
                failure.attempts,
                elapsed,
                exc,
            )
            return _UpdateOutcome.SKIPPED
        finally:
            _update_settled_hook.reset(hook_token)

    async def _commit_update(self, update_id: int) -> None:
        """update를 통과시킨다: 예산을 지우고 offset을 그 뒤로 옮겨 영속화한다 (#248, #350).

        보통은 배치 루프가 handle_update를 마친 뒤에 부른다. 결과 전송보다 먼저 확정해야 하는
        update는 핸들러가 처리 도중에 부른다(_update_settled_hook, #259 3단계).
        """
        self._failures.pop(update_id, None)
        self.offset = update_id + 1
        self._forget_passed_updates(self.offset)
        # 배치 끝이 아니라 update마다 쓴다. 배치 단위로 미루면 중간에 죽었을 때 이미 실행한
        # update의 기록이 통째로 사라져 영속화의 의미가 없다 (#248).
        #
        # 남는 창은 이 쓰기 전까지, 즉 handle_update의 실행 시간이다. 그 사이 죽으면 update가
        # 재배달·재실행된다. /confirm 체결 성공에서는 그 재실행이 claim에서 비어 "확정할 대기
        # 주문이 없습니다"로 체결을 오표시했다(#253 → #269). #259 2단계가 창을 최대 20초에서
        # send_text 한 번으로 줄였고, 3단계가 그 경로만 전송 전에 여기를 부르게 해 닫았다 —
        # test_confirmed_order_is_not_reexecuted_when_restart_lands_in_the_fill_send.
        #
        # 먼저 쓰는 대가(전송 전에 죽으면 메시지가 사라진다)는 원장이 받는다. 그 체결은 미통지로
        # 남아 scheduler.trade_notification.trade_notification_task가 배달한다. 나머지 settled 경로(/buy 프롬프트·
        # /cancel·/confirm 403·불명확)에는 받아줄 원장이 없어 창을 그대로 둔다 — 거기서 먼저
        # 쓰면 재실행이 내보내는 오표시 한 줄 대신 메시지 자체가 사라진다.
        #
        # 남긴 창은 수용 판단이다(#383). 발동 조건이 재시작과 settled 전송 창(최대
        # SETTLED_SEND_TIMEOUT_SECONDS, 20초)의 겹침이라 드물고, 피해가 유계다 — 주문 경로는
        # 오표시 한 줄(대기 주문 존재는 다음 /buy·/cancel로 드러난다), 자연어·/earnings·/advise는
        # LLM·제안 에이전트 재호출 한 번과 중복 답변이다. 경로마다 멱등 표지와 결과 재구성을 두는
        # 비용이 그 피해보다 크다. 재시작이 잦아지는 등 재과금이 커질 징후가 보이면 다시 본다.
        #
        # 예외가 하나 있다. 재실행된 텍스트 /confirm이 그사이 생긴 다른 대기 주문(자동 제안·새
        # /buy)을 claim하면 사용자가 본 적 없는 주문이 확정 없이 실행된다. 오표시로 유계가 아니라
        # 수용하지 않고, _handle_confirm이 update 표지와 프롬프트 message_id 대조(#386)로 막는다.
        await self._persist_state()

    def _retry_delay(self) -> float:
        """미해결 update 중 가장 적게 시도한 것을 기준으로 백오프 간격을 고른다 (#241).

        간격은 배치 전체에 하나뿐이라 가장 급한 쪽에 맞춰야 한다. 최대 시도 수를 쓰면
        갓 실패한 update가 오래된 poison의 45초 간격을 물려받아 자기 창(60초) 안에
        시도 횟수를 손해 본다 (PR #242 리뷰).
        """
        attempts = min((f.attempts for f in self._failures.values()), default=1)
        index = min(attempts, len(UPDATE_RETRY_BACKOFF_SECONDS)) - 1
        return UPDATE_RETRY_BACKOFF_SECONDS[index]

    async def _notify_updates_skipped(self, updates: list[dict[str, Any]]) -> None:
        # run()의 배치 루프는 try 밖이라 여기서 새는 예외는 폴러 태스크를 죽인다.
        # 통지문 조립(임의의 update dict를 파싱한다)까지 try 안에 둔다 (PR #242 리뷰).
        try:
            # notifier는 자기 chat에만 보낼 수 있으므로 다른 chat이면 통지할 방법이 없다.
            mine = [u for u in updates if _update_chat_id(u) == self.notifier.chat_id]
            if not mine:
                return
            labels = ", ".join(_update_label(u) for u in mine)
            sent = await self.notifier.send_text(
                f"{UPDATE_SKIPPED_NOTICE}\n실패한 요청: {labels}"
            )
        except Exception as exc:
            # 통지 실패가 폴러를 다시 막으면 안 된다. send_text는 예외를 삼키므로 여기
            # except는 duck-typed notifier 방어용이다 (#241).
            logger.error("Telegram skip notice send failed: %s", exc)
            return
        if sent is False:
            # send_text는 실패해도 예외 대신 False를 돌려준다. 유일한 실패 신호라
            # 반드시 확인해야 통지 실패가 어디에도 안 남는 일이 없다 (PR #242 리뷰).
            logger.error("Telegram skip notice not delivered for %s updates", len(mine))

    async def _restore_state(self) -> None:
        """재시작 전 offset과 재시도 예산을 복원한다 (#248, #350).

        실패하면 빈 상태로 시작한다. Telegram이 미확정 update를 전부 재배달하므로 명령이
        유실되지는 않지만, 실행됐던 것이 다시 실행된다 — 영속화 이전과 같은 상태다.
        여기서 예외를 올리면 redis 장애가 폴러 태스크의 죽음이 되므로 삼킨다.

        wait_for가 필요한 이유는 STATE_STORE_TIMEOUT_SECONDS 주석에 있다. 여기서는 hang이
        폴링 시작 자체를 막아, 봇이 아무 응답도 못 하는 상태로 무한정 머문다.
        """
        try:
            state = await asyncio.wait_for(
                self.state_store.load(), timeout=STATE_STORE_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # asyncio.TimeoutError는 3.11+에서 내장 TimeoutError(OSError 하위)라 여기 걸린다.
            # CancelledError는 BaseException이라 걸리지 않고 정상 전파된다.
            logger.error("Telegram poller 상태 복원 실패, 빈 상태로 시작: %s", exc)
            return
        self._state_restored = True
        self.offset = state.offset
        self._failures = self._restore_failures(state.failures)
        if state.offset is not None:
            # payload가 두 값의 관계를 지킨다는 보장은 없다 (PR #362 리뷰). 정상 쓰기 경로는
            # pop → offset 전진 → 저장 순이라 offset이 지나간 예산을 만들지 않지만,
            # _deserialize도 여기도 그 관계를 검증하지 않는다. 그런 항목은 재배달되지 않아
            # pop될 기회가 영영 없고, _retry_delay의 min(attempts)에 섞여 백오프만 줄인다.
            self._forget_passed_updates(state.offset)
            logger.info("Telegram poller 상태 복원: offset=%s", state.offset)
        for update_id, failure in self._failures.items():
            # 재시작을 넘긴 예산은 그 자체로 조사 대상이다 — 이 로그가 없으면 "재시작 직후
            # 첫 실패에서 바로 폐기됐다"가 원인 없는 사건으로 보인다.
            logger.info(
                "Telegram poller 재시도 예산 복원: update=%s attempts=%s elapsed=%.0fs",
                update_id,
                failure.attempts,
                self._now() - failure.first_at,
            )

    def _restore_failures(
        self, failures: tuple[TelegramPollerFailure, ...]
    ) -> dict[int, _UpdateFailure]:
        """저장된 벽시계 앵커를 이 프로세스의 monotonic 기준으로 환산한다 (#350).

        앵커가 미래면(= 벽시계가 뒤로 조정됐다) 경과를 0으로 접고 **앵커도 지금으로 다시
        찍는다**. 접기만 하면 예산이 한 번 더 지급되는 것으로 끝나지 않는다 (PR #362 리뷰):
        시계 오프셋은 재시작 뒤에도 그대로 남아 있으므로 저장된 앵커는 계속 미래고, 그러면
        **매 재시작이 같은 클램프를 반복해** #350이 없애려던 무기한 정지가 되살아난다.
        재각인하면 다음 재시작부터는 경과가 정상적으로 쌓여 폐기가 진행된다.

        min을 쓰는 이유는 반대 방향을 건드리지 않기 위해서다. 앵커가 과거면(정상·정방향
        점프) 저장된 값을 그대로 둔다 — 복원 시점으로 앞당기면 잦은 재시작이 예산을 무한히
        늘리는 #350의 경로가 그대로 되살아난다.

        경과가 창을 이미 넘겼어도 여기서 폐기하지는 않는다. 폐기 판정은 _handle_one_update의
        한 곳에 남겨 둔다 — 그 update가 재배달돼 다시 실패해야 폐기가 의미를 갖고, 재시작
        사이에 원인이 사라졌다면 그냥 성공하고 예산은 pop된다.
        """
        wall_now = self._wall_now()
        now = self._now()
        restored: dict[int, _UpdateFailure] = {}
        for failure in failures:
            anchor = min(failure.first_wall_at, wall_now)
            restored[failure.update_id] = _UpdateFailure(
                first_at=now - (wall_now - anchor),
                attempts=failure.attempts,
                send_failure=failure.send_failure,
                first_wall_at=anchor,
            )
        return restored

    async def _persist_state(self) -> None:
        """offset과 재시도 예산을 저장한다 (#248, #350).

        쓰기 실패는 삼킨다 — fail-open 근거는 RedisTelegramPollerStore 독스트링에 있다.
        폴링은 인메모리 상태로 계속되고, 다음 update의 쓰기가 성공하면 그 시점 상태가
        통째로 반영되므로 실패가 누적되지 않는다(전체 상태를 매번 덮어쓰는 덕분이다).

        wait_for가 없으면 이 삼킴이 무의미해진다 — 근거는 STATE_STORE_TIMEOUT_SECONDS 주석.
        여기 남는 로그가 "영속화가 조용히 죽었다"를 알리는 유일한 신호다. 이게 반복되면
        중복 창이 #248 이전 수준으로 돌아간 것이므로 경보 대상으로 삼을 만하다.
        """
        try:
            await asyncio.wait_for(
                self.state_store.save(
                    TelegramPollerState(
                        offset=self.offset, failures=self._failures_snapshot()
                    )
                ),
                timeout=STATE_STORE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # TimeoutError·CancelledError 취급은 _restore_state 주석 참조.
            logger.error("Telegram poller 상태 저장 실패, 인메모리로 계속: %s", exc)

    def _failures_snapshot(self) -> tuple[TelegramPollerFailure, ...]:
        """인메모리 예산을 저장 형식으로 옮긴다 — 나가는 시각은 벽시계다 (#350).

        update_id 순으로 정렬하는 이유는 payload를 재현 가능하게 만들기 위해서다. dict의
        삽입 순서가 그대로 나가면 같은 상태가 서로 다른 문자열이 되어, 저장된 값을 눈으로
        비교하거나 테스트가 payload를 고정하기 어려워진다.
        """
        return tuple(
            TelegramPollerFailure(
                update_id=update_id,
                first_wall_at=failure.first_wall_at,
                attempts=failure.attempts,
                send_failure=failure.send_failure,
            )
            for update_id, failure in sorted(self._failures.items())
        )

    def _forget_passed_updates(self, offset: int) -> None:
        """offset이 지나간 update_id 기록을 정리해 추적 상태가 무한히 커지지 않게 한다 (#241).

        #259 1단계 이후 이 정리는 도달 가능한 모든 상태에서 no-op이다. _failures에 항목을
        새로 넣는 것은 배치를 끊는 RETRY 하나뿐이고, 그 항목은 offset이 지나가지 못한 가장
        작은 id라 다음 배치의 맨 앞으로 정렬된다 — 거기서 pop되거나 다시 배치를 끊으므로,
        유일한 호출부(그 pop 바로 다음 줄)가 보는 dict는 항상 비어 있다.

        그래도 남기는 이유는 skip 통지 병합(_notify_updates_skipped 호출부 주석)과 같다.
        "항목이 최대 1개"는 배치 루프의 모양에 딸린 성질이라 루프가 다시 바뀌면 무한 증가가
        되살아나는데, 그것을 막는 코드는 여기뿐이다. 정리 자체는
        test_forget_passed_updates_drops_only_what_the_offset_passed가 helper 단위로 고정한다.
        """
        self._failures = {
            update_id: failure
            for update_id, failure in self._failures.items()
            if update_id >= offset
        }

    async def _sleep(self, seconds: float) -> None:
        """테스트가 전역 asyncio.sleep 대신 이 인스턴스만 대체할 수 있게 하는 간접층 (#241).

        전역 패치는 핸들러 내부의 실제 sleep(_handle_watch의 조회 간격)까지 가로채
        루프가 엉뚱한 지점에서 끊긴 채 테스트가 통과할 수 있다 (PR #242 리뷰).
        """
        await asyncio.sleep(seconds)

    def _now(self) -> float:
        """재시도 예산용 단조 시계. 테스트에서 대체할 수 있도록 메서드로 둔다 (#241)."""
        return time.monotonic()

    def _wall_now(self) -> float:
        """예산을 재시작 너머로 나르기 위한 벽시계 (#350).

        _now와 갈라 두는 이유는 _UpdateFailure 독스트링에 있다. 여기 값은 영속화 payload와
        복원 시 경과 계산에만 쓰이고, 살아 있는 프로세스 안의 판정에는 쓰이지 않는다.
        """
        return time.time()

    async def _setup_bot_profile(self) -> None:
        # notifier는 주입 가능하고 이 능력은 선택 사항이라, 선언 타입이 아니라
        # 런타임 존재 여부로 판정한다. 선언 타입에 없는 속성이라 getattr의 결과는
        # object로 추론되는데, 그러면 callable() 통과 뒤에도 await가 막힌다.
        load_bot_username: Callable[..., Any] | None = getattr(
            self.notifier, "load_bot_username", None
        )
        if callable(load_bot_username):
            try:
                await load_bot_username()
            except Exception as exc:
                logger.error("Telegram bot username setup failed: %s", exc)

        set_bot_commands: Callable[..., Any] | None = getattr(
            self.notifier, "set_bot_commands", None
        )
        if callable(set_bot_commands):
            try:
                await set_bot_commands(TELEGRAM_BOT_COMMANDS)
            except Exception as exc:
                logger.error("Telegram bot command menu setup failed: %s", exc)

    async def _get_updates(self) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 25, "limit": GET_UPDATES_LIMIT}
        if self.offset is not None:
            payload["offset"] = self.offset

        # URL을 직접 만들지 않는다. 여기서 만들면 raise_for_status의 예외에 토큰이 실려
        # 아래 run()의 "polling failed" 로그로 흘러간다 — 실제로 그랬다 (PR #253 2차 리뷰).
        # fetch_telegram_api가 URL 없는 TelegramApiError로 바꿔 던진다 (#257).
        body = await fetch_telegram_api(
            self.notifier.bot_token, "getUpdates", payload=payload, timeout=30.0
        )
        if body.get("ok") is not True:
            return []
        return body.get("result") or []


def start_telegram_commands() -> None:
    global _telegram_command_task
    if not telegram_notifier.enabled or _telegram_command_task is not None:
        return
    poller = TelegramCommandPoller()
    _telegram_command_task = asyncio.create_task(poller.run())


async def stop_telegram_commands() -> None:
    global _telegram_command_task
    if _telegram_command_task is None:
        return
    _telegram_command_task.cancel()
    try:
        await _telegram_command_task
    except asyncio.CancelledError:
        pass
    finally:
        _telegram_command_task = None
