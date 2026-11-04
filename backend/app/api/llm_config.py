"""Tenant LLM configuration, connection test and usage routes (BYOK).

TenantAdmin / PlatformAdmin only; the tenant always comes from the gateway
headers, never from the body. These routes receive a raw API key on PUT, so:

- the body is parsed here and not by FastAPI, because FastAPI's default 422
  body echoes the submitted input (the key included);
- no response, log line or audit row carries the key or its ciphertext;
- error bodies are `{"detail": {"code": ...}}` with fixed codes.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, ValidationError

from app.agent.loop import TurnResult
from app.agent.quota import PANEL_CHANNEL
from app.audit.repository import record_turn
from app.config import get_settings
from app.db import get_pool
from app.domain.session import SessionContext
from app.llm import configs, keys
from app.llm import provider as llm_provider
from app.llm.allowlist import parse
from app.llm.resolver import tenant_provider
from app.middleware import AuthContext, require_roles

logger = logging.getLogger(__name__)

router = APIRouter(tags=["llm-config"])

_admin = require_roles("TenantAdmin", "PlatformAdmin")

_MODEL_MAX_LEN = 200
_KEY_MIN_LEN = 8
_TEST_MAX_TOKENS = 8
_TEST_PROMPT = "Reply with: ok"
# What the audit stores for a panel test: never the model output.
_TEST_REPLY_MARKER = "[panel test]"


def _err(code: int, name: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": name})


class LLMConfigBody(BaseModel):
    provider: str
    model: str
    api_base: str | None = None
    api_key: str | None = None
    max_tokens_per_turn: int | None = Field(default=None, gt=0)


def _view(row: configs.TenantLLMConfig | None, notice: str | None) -> dict:
    settings = get_settings()
    return {
        "byok_available": keys.available(),
        "configured": row is not None,
        "provider": row.provider if row else None,
        "model": row.model if row else None,
        "api_base": row.api_base if row else None,
        "key_last4": row.api_key_last4 if row else None,
        "max_tokens_per_turn": row.max_tokens_per_turn if row else None,
        "ceiling": settings.max_tokens_per_turn_ceiling,
        "allowed": parse(settings.llm_endpoint_allowlist).entries(),
        "notice": notice,
    }


async def _current_view(tenant_id: str) -> dict:
    row = await configs.get(tenant_id)
    notice = None
    if row is not None:
        resolved = await tenant_provider(tenant_id)
        notice = resolved.notice if resolved is not None else None
    return _view(row, notice)


def _valid_model(model: str) -> bool:
    if not model or len(model) > _MODEL_MAX_LEN:
        return False
    # "://" would let some LiteLLM providers use the model as the request URL,
    # bypassing the endpoint allowlist.
    if "://" in model:
        return False
    return not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in model)


@router.get("/llm-config")
async def get_llm_config(user: AuthContext = Depends(_admin)) -> dict:
    return await _current_view(user.tenant_id)


@router.put("/llm-config")
async def put_llm_config(
    request: Request, user: AuthContext = Depends(_admin)
) -> dict:
    # Parsed by hand: see the module docstring. Errors name fields, never values.
    try:
        raw = await request.json()
        body = LLMConfigBody.model_validate(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise HTTPException(
            status_code=422, detail={"code": "invalid_body", "fields": fields}
        ) from None
    except ValueError:
        raise _err(422, "invalid_body") from None

    settings = get_settings()
    if not keys.available():
        raise _err(503, "byok_unavailable")

    provider = body.provider.strip().lower()
    if not _valid_model(body.model):
        raise _err(422, "invalid_model")
    api_key = body.api_key.strip() if body.api_key else None
    if api_key and len(api_key) < _KEY_MIN_LEN:
        raise _err(422, "invalid_key")
    api_base = body.api_base.strip() if body.api_base else None
    api_base = api_base or None

    if not parse(settings.llm_endpoint_allowlist).allows(provider, api_base):
        raise _err(422, "endpoint_not_allowed")
    if (
        body.max_tokens_per_turn is not None
        and body.max_tokens_per_turn > settings.max_tokens_per_turn_ceiling
    ):
        raise _err(422, "budget_above_ceiling")

    try:
        await configs.upsert(
            user.tenant_id,
            provider=provider,
            model=body.model,
            api_base=api_base,
            api_key=api_key or None,
            max_tokens_per_turn=body.max_tokens_per_turn,
            updated_by=user.user_id,
        )
    except configs.KeyRequired:
        raise _err(422, "key_required") from None
    except configs.BYOKUnavailable:
        raise _err(503, "byok_unavailable") from None
    return await _current_view(user.tenant_id)


@router.delete("/llm-config", status_code=status.HTTP_204_NO_CONTENT)
async def delete_llm_config(user: AuthContext = Depends(_admin)) -> Response:
    await configs.delete(user.tenant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/llm-config/test")
async def test_llm_config(
    request: Request, user: AuthContext = Depends(_admin)
) -> dict:
    """One minimal completion with the saved config, audited on channel panel."""
    trace_id = uuid.uuid4().hex
    ctx = SessionContext(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        roles=tuple(user.roles),
        channel=PANEL_CHANNEL,
        channel_user_id=request.headers.get("X-User-ID") or user.user_id,
        trace_id=trace_id,
    )
    started = time.monotonic()
    ok = False
    error: str | None = None
    detail: str | None = None
    outcome = "error"
    model: str | None = None
    origin: str | None = "none"
    prompt_tokens = completion_tokens = 0
    try:
        reply = await llm_provider.complete(
            [{"role": "user", "content": _TEST_PROMPT}],
            tenant_id=user.tenant_id,
            max_tokens=_TEST_MAX_TOKENS,
        )
        ok, outcome = True, "ok"
        model, origin = reply.model, reply.origin
        prompt_tokens, completion_tokens = reply.tokens_prompt, reply.tokens_completion
    except llm_provider.LLMUnconfigured as exc:
        outcome = "unconfigured"
        error = exc.notice or "not_configured"
    except llm_provider.LLMProviderError as exc:
        origin = exc.origin or "none"
        # The TenantAdmin sees why the test failed: a fixed code plus the
        # provider's message, already stripped of the key and capped. Neither
        # goes to the audit row or to any chat reply.
        error = exc.kind
        detail = exc.detail
    except Exception:
        logger.exception("llm_config_test_failed tenant_id=%s", user.tenant_id)
        error = "provider_error"

    result = TurnResult(
        text=_TEST_REPLY_MARKER,
        outcome=outcome,
        model=model,
        tool_calls=(),
        tokens_prompt=prompt_tokens,
        tokens_completion=completion_tokens,
        llm_origin=origin,
    )
    await record_turn(
        ctx, None, result, int((time.monotonic() - started) * 1000), trace_id
    )
    return {"ok": True} if ok else {"ok": False, "error": error, "detail": detail}


def _cost(model: str | None, prompt: int, completion: int) -> float | None:
    """USD estimate from LiteLLM's price table; None when it cannot say."""
    if not prompt and not completion:
        return 0.0
    if not model:
        return None
    return llm_provider.estimate_cost(model, prompt, completion)


def _sum_cost(costs: list[float | None]) -> float | None:
    if any(c is None for c in costs):
        return None
    return round(sum(costs), 6)  # type: ignore[arg-type]


@router.get("/usage")
async def get_usage(user: AuthContext = Depends(_admin)) -> dict:
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    end = (
        datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        if now.month == 12
        else datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    )
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant', $1, true)", user.tenant_id
        )
        rows = await conn.fetch(
            """
            SELECT (created_at AT TIME ZONE 'UTC')::date AS day, model,
                   count(*) FILTER (WHERE outcome <> 'refused' AND channel <> $4) AS turns,
                   coalesce(sum(tokens_prompt), 0)::bigint AS tp,
                   coalesce(sum(tokens_completion), 0)::bigint AS tc
              FROM agent_turn_audit
             WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
             GROUP BY 1, 2
             ORDER BY 1
            """,
            user.tenant_id, start, end, PANEL_CHANNEL,
        )

    by_day: dict[date, dict] = {}
    for r in rows:
        d = by_day.setdefault(
            r["day"], {"turns": 0, "tp": 0, "tc": 0, "costs": []}
        )
        d["turns"] += r["turns"]
        d["tp"] += r["tp"]
        d["tc"] += r["tc"]
        d["costs"].append(_cost(r["model"], r["tp"], r["tc"]))

    days = []
    for day in sorted(by_day):
        d = by_day[day]
        days.append(
            {
                "date": day.isoformat(),
                "turns": d["turns"],
                "tokens_prompt": d["tp"],
                "tokens_completion": d["tc"],
                "cost_estimate_usd": _sum_cost(d["costs"]),
            }
        )
    totals = {
        "turns": sum(x["turns"] for x in days),
        "tokens_prompt": sum(x["tokens_prompt"] for x in days),
        "tokens_completion": sum(x["tokens_completion"] for x in days),
        "cost_estimate_usd": _sum_cost([x["cost_estimate_usd"] for x in days]),
    }
    return {"month": f"{now.year:04d}-{now.month:02d}", "days": days, "totals": totals}
