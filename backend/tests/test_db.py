import pytest

from app.db import close_pool, get_pool
from tests.conftest import requires_db


@requires_db
@pytest.mark.asyncio
async def test_get_pool_is_a_singleton():
    a = await get_pool()
    b = await get_pool()
    assert a is b
    await close_pool()


@pytest.mark.asyncio
async def test_pool_requires_postgres_url(monkeypatch):
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    await close_pool()
    with pytest.raises(RuntimeError, match="POSTGRES_URL"):
        await get_pool()
    get_settings.cache_clear()
