import pytest

from backend import services, stock_code


@pytest.fixture(autouse=True)
def _clear_stock_code_cache():
    # 이 픽스처가 막는 것은 캐시 오염(테스트 간 종목코드 캐시 누수)뿐이다.
    # 어떤 테스트가 run_mcp_tool을 몽키패치하지 않아 실제 MCP stdio 서브프로세스를
    # 띄우는 회귀는 이 픽스처로 잡히지 않고, `-s` 없이는 그 서브프로세스 실행 자체도
    # 화면에 보이지 않는다. 실제 방어는 test_database.py의 run_mcp_tool 몽키패치다.
    services._stock_code_cache.clear()
    yield
    services._stock_code_cache.clear()


@pytest.fixture(autouse=True)
def _clear_master_code_cache():
    # #151: _is_known_master_code의 지연 로딩 캐시도 테스트 간에 새면 안 된다 —
    # 한 테스트가 _TRADING_MCP_DIR을 몽키패치해 로드 실패(fail-open)를
    # 캐시해 버리면, 몽키패치가 되돌아간 뒤에도 다음 테스트가 그 실패 상태를
    # 그대로 물려받아 실제 마스터 대조를 건너뛰게 된다.
    # _master_codes_cache_path는 _master_codes_cache가 None이면 어차피 무시되지만
    # (첫 조건에서 단락 평가), 둘을 짝으로 리셋해 상태를 명확히 남긴다.
    stock_code._master_codes_cache = None
    stock_code._master_codes_cache_path = None
    yield
    stock_code._master_codes_cache = None
    stock_code._master_codes_cache_path = None
