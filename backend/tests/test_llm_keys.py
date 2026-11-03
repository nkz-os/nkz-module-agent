"""Encryption of tenant LLM keys (app/llm/keys.py).

Keys are read from settings on every call (no module-level cache), so each
test can switch LLM_KEY_ENCRYPTION_KEYS via the environment.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.llm import keys


def _set_keys(monkeypatch, value: str) -> None:
    monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEYS", value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    get_settings.cache_clear()


def test_roundtrip(monkeypatch):
    _set_keys(monkeypatch, Fernet.generate_key().decode())
    token = keys.encrypt("sk-secret-value-a1b2")
    assert isinstance(token, bytes)
    assert b"sk-secret" not in token
    assert keys.decrypt(token) == "sk-secret-value-a1b2"


def test_rotation_decrypts_old_and_encrypts_with_first(monkeypatch):
    a = Fernet.generate_key().decode()
    b = Fernet.generate_key().decode()
    _set_keys(monkeypatch, a)
    old = keys.encrypt("sk-rotate-me")

    _set_keys(monkeypatch, f"{b},{a}")
    assert keys.decrypt(old) == "sk-rotate-me"
    new = keys.reencrypt(old)
    assert new != old

    # Only B is needed to read the re-encrypted token: first key encrypts.
    _set_keys(monkeypatch, b)
    assert keys.decrypt(new) == "sk-rotate-me"
    with pytest.raises(keys.KeyUnreadable):
        keys.decrypt(old)


def test_unavailable_without_keys(monkeypatch):
    _set_keys(monkeypatch, "")
    assert keys.available() is False
    with pytest.raises(keys.KeyUnreadable):
        keys.encrypt("sk-x")
    with pytest.raises(keys.KeyUnreadable):
        keys.decrypt(b"anything")


def test_wrong_key_raises_key_unreadable(monkeypatch):
    _set_keys(monkeypatch, Fernet.generate_key().decode())
    token = keys.encrypt("sk-secret-zzzz")
    _set_keys(monkeypatch, Fernet.generate_key().decode())
    with pytest.raises(keys.KeyUnreadable) as exc:
        keys.decrypt(token)
    assert "sk-secret" not in str(exc.value)
    assert token.decode() not in str(exc.value)


def test_garbage_ciphertext_is_key_unreadable(monkeypatch):
    _set_keys(monkeypatch, Fernet.generate_key().decode())
    with pytest.raises(keys.KeyUnreadable):
        keys.decrypt(b"not-a-fernet-token")


def test_invalid_master_key_is_unavailable(monkeypatch):
    _set_keys(monkeypatch, "not-a-valid-fernet-key")
    assert keys.available() is False


def test_available_with_key(monkeypatch):
    _set_keys(monkeypatch, Fernet.generate_key().decode())
    assert keys.available() is True


def test_last4():
    assert keys.last4("sk-abcdef-a1b2") == "a1b2"
    assert keys.last4("ab") == "ab"
    assert keys.last4("  sk-xyz9 \n") == "xyz9"


@pytest.mark.asyncio
async def test_startup_logs_critical_without_keys_and_does_not_exit(monkeypatch, caplog):
    import logging

    from app.main import create_app, lifespan

    monkeypatch.setenv("POSTGRES_URL", "postgresql://u:p@localhost/db")
    _set_keys(monkeypatch, "")
    app = create_app()
    with caplog.at_level(logging.CRITICAL, logger="app.main"):
        async with lifespan(app):
            pass
    assert any("byok_unavailable" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_silent_with_keys(monkeypatch, caplog):
    import logging

    from app.main import create_app, lifespan

    monkeypatch.setenv("POSTGRES_URL", "postgresql://u:p@localhost/db")
    _set_keys(monkeypatch, Fernet.generate_key().decode())
    app = create_app()
    with caplog.at_level(logging.INFO, logger="app.main"):
        async with lifespan(app):
            pass
    assert not any("byok_unavailable" in r.getMessage() for r in caplog.records)
