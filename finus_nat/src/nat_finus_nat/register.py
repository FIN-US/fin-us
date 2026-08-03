# agents, finus_api 모듈을 import하여 `@register_function` 등록을 로드한다.
from pathlib import Path

from dotenv import load_dotenv

# finus_nat 패키지 루트의 .env 파일을 찾아 환경 변수에 로드한다.
# register.py -> nat_finus_nat/ -> src/ -> finus_nat/
_FINUS_NAT_ENV = Path(__file__).resolve().parents[2] / ".env"
if _FINUS_NAT_ENV.is_file():
    load_dotenv(_FINUS_NAT_ENV, override=False)

#  NAT에 함수/도구 등록을 트리거
from nat_finus_nat import agents
from nat_finus_nat import finus_api
