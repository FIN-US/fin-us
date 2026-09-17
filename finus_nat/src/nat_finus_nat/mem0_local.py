"""mem0 OSS를 이 프로세스 안에서 로컬 스토리지 모드로 띄운다 (#397).

NAT의 ``mem0_memory``(``nat.plugins.mem0ai``)는 ``AsyncMemoryClient``, 즉 **HTTP 클라이언트**다.
host를 비우면 ``https://api.mem0.ai``(클라우드)로 가고, host를 채워도 별도 self-hosted 서버가
필요하며 그 서버의 임베딩·추출 LLM은 대시보드에 넣은 OpenAI 키로 돈다. 로컬 우선 원칙에 맞지
않아 그 경로를 쓰지 않고, mem0 라이브러리의 ``AsyncMemory``를 프로세스 안에서 직접 띄운다.

저장소는 전부 로컬 파일이다.

- 벡터: qdrant **로컬 모드**(``path`` + ``on_disk``). 서버가 아니라 파일이다. ``on_disk``를 끄면
  mem0가 기동할 때마다 그 디렉터리를 지우므로(``mem0/vector_stores/qdrant.py``) 반드시 켠다.
- 변경 이력: sqlite 파일.

외부로 나갈 수 있는 경로 셋을 코드로 막는다. 설정값이 아니라 코드인 이유는, 설정은 한 줄만
바뀌어도 조용히 외부 전송이 생기기 때문이다.

1. **텔레메트리** — mem0는 import 시점에 PostHog(``us.i.posthog.com``) 클라이언트를 만들고
   ``add``/``get_all``/``delete``마다 이벤트를 보낸다(기본 켜짐). 환경변수와 모듈 속성을 함께
   끈다: 환경변수는 import 전에만 효과가 있고, 이미 import된 뒤라면 속성을 바꿔야 한다.
2. **임베더** — mem0 기본값은 OpenAI 임베딩이다. 저장 내용이 허용목록의 선호 값뿐이고 읽기는
   벡터 검색이 아니라 user_id 필터 조회라 의미 임베딩이 필요 없다. 네트워크를 쓰지 않는
   해시 임베더로 바꾼다.
3. **추출 LLM** — ``infer=True``면 mem0가 LLM으로 사실을 추출한다. 쓰기는 항상 ``infer=False``로
   부르지만, 그 약속이 깨져도 나가지 않도록 호출되면 예외를 던지는 LLM을 끼운다.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any

# import 전에 둬야 mem0.memory.telemetry가 읽는다. setdefault가 아니라 덮어쓴다 —
# 공용 .env에 MEM0_TELEMETRY=True가 들어와도 이 프로세스에서는 켜지지 않아야 한다.
os.environ["MEM0_TELEMETRY"] = "False"

LOCAL_EMBEDDER_PROVIDER = "finus_local_hash"
REFUSING_LLM_PROVIDER = "finus_refusing_llm"
EMBEDDING_DIMS = 64


def _no_telemetry(*_args: Any, **_kwargs: Any) -> None:
    return None


def silence_mem0_telemetry() -> None:
    """mem0가 이미 import됐든 아니든 텔레메트리 전송을 끈다. 여러 번 불러도 된다."""
    os.environ["MEM0_TELEMETRY"] = "False"
    import mem0.client.main as client_main
    import mem0.memory.main as memory_main
    import mem0.memory.telemetry as telemetry

    telemetry.MEM0_TELEMETRY = False
    telemetry.client_telemetry.posthog.disabled = True
    # main 모듈은 capture_event를 이름으로 import해 쓴다 — telemetry 모듈의 함수를 바꿔도
    # 이미 묶인 이름은 그대로라 호출부 모듈의 이름을 바꾼다. 이벤트마다 AnonymousTelemetry를
    # 새로 만들며 벡터 저장소에 식별용 레코드를 끼워 넣는 부작용도 함께 사라진다.
    memory_main.capture_event = _no_telemetry
    client_main.capture_client_event = _no_telemetry


silence_mem0_telemetry()

from mem0 import AsyncMemory  # noqa: E402 — 텔레메트리를 끈 뒤에 import한다
from mem0.configs.base import MemoryConfig  # noqa: E402
from mem0.configs.llms.base import BaseLlmConfig  # noqa: E402
from mem0.embeddings.base import EmbeddingBase  # noqa: E402
from mem0.embeddings.configs import EmbedderConfig  # noqa: E402
from mem0.llms.base import LLMBase  # noqa: E402
from mem0.llms.configs import LlmConfig  # noqa: E402
from mem0.utils.factory import EmbedderFactory  # noqa: E402
from mem0.utils.factory import LlmFactory  # noqa: E402


class LocalHashEmbedding(EmbeddingBase):
    """문자 bigram 해시를 고정 차원에 누적한 결정적 임베딩. 네트워크를 쓰지 않는다."""

    def embed(self, text, memory_action=None):  # noqa: ARG002 — mem0 인터페이스
        vector = [0.0] * EMBEDDING_DIMS
        normalized = str(text).casefold()
        grams = [normalized[i : i + 2] for i in range(max(len(normalized) - 1, 1))]
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            vector[digest[0] % EMBEDDING_DIMS] += 1.0 if digest[1] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]


class RefusingLlm(LLMBase):
    """호출되면 실패한다. 추출 LLM 경로가 열리면 외부 전송 대신 예외로 드러나게 한다."""

    def generate_response(self, messages, tools=None, tool_choice="auto", **kwargs):  # noqa: ARG002
        raise RuntimeError(
            "Fin-Us 로컬 메모리는 LLM 추출(infer=True)을 쓰지 않습니다. 쓰기는 infer=False로만 해야 합니다."
        )


def _register_local_providers() -> None:
    # mem0의 설정 모델은 provider 이름을 고정 목록으로 검증하므로, 팩토리에만 등록하고 설정은
    # 검증을 거치지 않는 model_construct로 만든다(아래 build_local_async_memory).
    EmbedderFactory.provider_to_class[LOCAL_EMBEDDER_PROVIDER] = f"{__name__}.LocalHashEmbedding"
    LlmFactory.provider_to_class[REFUSING_LLM_PROVIDER] = (f"{__name__}.RefusingLlm", BaseLlmConfig)


def build_local_async_memory(storage_dir: Path, collection_name: str) -> AsyncMemory:
    """*storage_dir* 아래 파일만 쓰는 ``AsyncMemory``를 만든다."""
    silence_mem0_telemetry()
    _register_local_providers()
    storage_dir.mkdir(parents=True, exist_ok=True)

    config = MemoryConfig(
        vector_store={
            "provider": "qdrant",
            "config": {
                "collection_name": collection_name,
                "embedding_model_dims": EMBEDDING_DIMS,
                "path": str(storage_dir / "qdrant"),
                "on_disk": True,
            },
        },
        history_db_path=str(storage_dir / "history.db"),
    )
    config.embedder = EmbedderConfig.model_construct(
        provider=LOCAL_EMBEDDER_PROVIDER, config={"embedding_dims": EMBEDDING_DIMS}
    )
    config.llm = LlmConfig.model_construct(provider=REFUSING_LLM_PROVIDER, config={})
    return AsyncMemory(config)
