import pytest

from backend import services


@pytest.fixture(autouse=True)
def _clear_stock_code_cache():
    services._stock_code_cache.clear()
    yield
    services._stock_code_cache.clear()
