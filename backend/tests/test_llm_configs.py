import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.llm import configs, keys
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _save(tenant="tenant-a", **over):
    args = dict(
        provider="openai", model="m1", api_base="https://llm.example.test/v1",
        api_key="sk-test-a1b2", max_tokens_per_turn=500, updated_by="u1",
    )
    args.update(over)
    return await configs.upsert(tenant, **args)


async def test_insert_get_delete(db_pool):
    assert await configs.get("tenant-a") is None
    saved = await _save()
    got = await configs.get("tenant-a")
    assert got == saved
    assert got.api_key_last4 == "a1b2"
    assert got.max_tokens_per_turn == 500
    assert keys.decrypt(got.api_key_ciphertext) == "sk-test-a1b2"
    assert await configs.delete("tenant-a") is True
    assert await configs.get("tenant-a") is None
    assert await configs.delete("tenant-a") is False


async def test_first_save_without_key_requires_key(db_pool):
    with pytest.raises(configs.KeyRequired):
        await _save(api_key=None)
    assert await configs.get("tenant-a") is None


async def test_blank_key_counts_as_no_key(db_pool):
    await _save()
    with pytest.raises(configs.KeyRequired):
        await _save(api_key="  ", provider="other")


async def test_provider_change_requires_key(db_pool):
    await _save()
    with pytest.raises(configs.KeyRequired):
        await _save(api_key=None, provider="mistral")
    assert (await configs.get("tenant-a")).provider == "openai"


async def test_api_base_change_requires_key(db_pool):
    await _save()
    with pytest.raises(configs.KeyRequired):
        await _save(api_key=None, api_base="https://other.example.test/v1")
    with pytest.raises(configs.KeyRequired):
        await _save(api_key=None, api_base=None)


async def test_equivalent_api_base_and_provider_case_keep_key(db_pool):
    first = await _save()
    again = await _save(
        api_key=None, provider="OpenAI", api_base="  HTTPS://LLM.example.test:443/other ",
    )
    assert again.api_key_ciphertext == first.api_key_ciphertext
    assert again.api_base == "HTTPS://LLM.example.test:443/other"


async def test_model_and_budget_change_keep_ciphertext(db_pool):
    first = await _save()
    again = await _save(api_key=None, model="m2", max_tokens_per_turn=None, updated_by="u2")
    assert again.model == "m2"
    assert again.max_tokens_per_turn is None
    assert again.updated_by == "u2"
    assert again.api_key_ciphertext == first.api_key_ciphertext
    assert again.api_key_last4 == "a1b2"


async def test_new_key_replaces_ciphertext_and_blank_api_base_is_none(db_pool):
    await _save()
    again = await _save(api_key="sk-new-zz99", provider="mistral", api_base="   ")
    assert again.api_base is None
    assert again.api_key_last4 == "zz99"
    assert keys.decrypt(again.api_key_ciphertext) == "sk-new-zz99"


async def test_tenant_isolation(db_pool):
    await _save("tenant-a")
    assert await configs.get("tenant-b") is None
    assert await configs.delete("tenant-b") is False
    assert await configs.get("tenant-a") is not None


async def test_unavailable_encryption_stores_nothing(db_pool, monkeypatch):
    monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEYS", "")
    get_settings.cache_clear()
    with pytest.raises(configs.BYOKUnavailable):
        await _save()
    assert await configs.get("tenant-a") is None
