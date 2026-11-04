"""Master-key rotation script (scripts/rotate_llm_keys.py), against real Postgres."""

from __future__ import annotations

import importlib.util
import os
import pathlib
from urllib.parse import urlsplit, urlunsplit

import asyncpg

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.llm import keys
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rotate_llm_keys.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rotate_llm_keys", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_keys(monkeypatch, value: str) -> None:
    monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEYS", value)
    get_settings.cache_clear()


async def test_rotation_reencrypts_every_row_and_is_idempotent(db_pool, monkeypatch):
    a = Fernet.generate_key().decode()
    b = Fernet.generate_key().decode()
    _set_keys(monkeypatch, a)
    secrets = {"tenant-a": "sk-aaaa-1111", "tenant-b": "sk-bbbb-2222"}
    async with db_pool.acquire() as conn:
        for tenant, secret in secrets.items():
            await conn.execute(
                "INSERT INTO tenant_llm_configs (tenant_id, provider, model, "
                "api_key_ciphertext, api_key_last4, updated_by) "
                "VALUES ($1,'p','m',$2,$3,'u')",
                tenant, keys.encrypt(secret), keys.last4(secret),
            )

    _set_keys(monkeypatch, f"{b},{a}")
    script = _load_script()
    rotated, unreadable = await script.rotate(os.environ["POSTGRES_URL"])
    assert (rotated, unreadable) == (2, 0)

    async with db_pool.acquire() as conn:
        after_first = {
            r["tenant_id"]: bytes(r["api_key_ciphertext"])
            for r in await conn.fetch(
                "SELECT tenant_id, api_key_ciphertext FROM tenant_llm_configs"
            )
        }

    # B alone now reads every row.
    _set_keys(monkeypatch, b)
    for tenant, secret in secrets.items():
        assert keys.decrypt(after_first[tenant]) == secret

    # Second run: nothing to do, ciphertext untouched.
    _set_keys(monkeypatch, f"{b},{a}")
    assert await script.rotate(os.environ["POSTGRES_URL"]) == (0, 0)
    async with db_pool.acquire() as conn:
        after_second = {
            r["tenant_id"]: bytes(r["api_key_ciphertext"])
            for r in await conn.fetch(
                "SELECT tenant_id, api_key_ciphertext FROM tenant_llm_configs"
            )
        }
    assert after_second == after_first


async def test_rotation_counts_unreadable_rows_and_leaves_them(db_pool, monkeypatch):
    _set_keys(monkeypatch, Fernet.generate_key().decode())
    orphan = keys.encrypt("sk-orphan-0000")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_llm_configs (tenant_id, provider, model, "
            "api_key_ciphertext, api_key_last4, updated_by) "
            "VALUES ('tenant-a','p','m',$1,'0000','u')",
            orphan,
        )
    _set_keys(monkeypatch, Fernet.generate_key().decode())
    script = _load_script()
    assert await script.rotate(os.environ["POSTGRES_URL"]) == (0, 1)
    async with db_pool.acquire() as conn:
        assert bytes(await conn.fetchval(
            "SELECT api_key_ciphertext FROM tenant_llm_configs"
        )) == orphan


async def test_rotation_refuses_role_without_rls_bypass(db_pool, monkeypatch):
    _set_keys(monkeypatch, Fernet.generate_key().decode())
    role = "agent_rotate_probe"
    async with db_pool.acquire() as conn:
        await conn.execute(f"DROP ROLE IF EXISTS {role}")
        await conn.execute(
            f"CREATE ROLE {role} LOGIN PASSWORD 'probe' NOSUPERUSER NOBYPASSRLS"
        )
    try:
        parts = urlsplit(os.environ["POSTGRES_URL"])
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        dsn = urlunsplit(parts._replace(netloc=f"{role}:probe@{host}"))
        with pytest.raises(SystemExit) as exc:
            await _load_script().rotate(dsn)
        assert "Refusing" in str(exc.value)
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(f"DROP ROLE IF EXISTS {role}")
