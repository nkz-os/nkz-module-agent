"""Mobile channel: asynchronous agent turns for the app.

POST creates a queued turn and schedules it; the app polls GET until it is
terminal. Identity comes only from the gateway headers; error bodies are
`{"detail": {"code": ...}}` (422 validation adds `fields`, never the input).
The one exception is 401 from `require_auth` (auth layer): a plain detail string.
"""

from __future__ import annotations

import logging
import re
import uuid
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from nkz_platform_sdk.orion import OrionClient
from pydantic import BaseModel, Field

from app.agent import quota
from app.config import get_settings
from app.domain.session import SessionContext
from app.middleware import AuthContext, get_current_user
from app.mobile import turns
from app.mobile.runner import run_mobile_turn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mobile"])

MOBILE_CHANNEL = "mobile"
_PARCEL_RE = re.compile(r"^urn:ngsi-ld:AgriParcel:[A-Za-z0-9._:-]{1,120}$")
_PARCEL_TYPES = ("AgriParcel", "https://saref.etsi.org/saref4agri/AgriParcel")
_NAME_MAX = 80
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def _err(code: int, name: str, **extra) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": name, **extra})


class TurnCreate(BaseModel):
    text: str
    client_turn_id: str = Field(min_length=1, max_length=64)
    parcel_id: str | None = None
    lang: str = Field(default="es", max_length=16)


def _view(t: turns.MobileTurn) -> dict:
    return {
        "turn_id": t.turn_id,
        "state": t.state,
        "text": t.text,
        "reply_text": t.reply_text,
        "citations": [
            {
                "source": c.get("source"),
                "retrieved_at": c.get("retrieved_at"),
                "detail": c.get("detail"),
            }
            for c in t.citations
        ],
        "outcome": t.outcome,
        "error_code": t.error_code,
        "parcel_id": t.parcel_id,
        "created_at": t.created_at.isoformat(),
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
    }


def _name_of(entity: dict, fallback: str) -> str:
    """Tenant-written text: single line, no control chars, capped."""
    raw = entity.get("name")
    if isinstance(raw, dict):
        raw = raw.get("value")
    if not isinstance(raw, str):
        return fallback
    clean = _CONTROL_RE.sub(" ", raw).replace('"', "'").strip()[:_NAME_MAX].strip()
    return clean or fallback


async def _parcel_context(tenant_id: str, parcel_id: str) -> str:
    """Prove the parcel is in the tenant; any miss or broker error is a 404."""
    if not _PARCEL_RE.fullmatch(parcel_id):
        raise _err(422, "invalid_parcel")
    client = OrionClient(tenant_id)
    try:
        entity = await client.get_entity(quote(parcel_id, safe=":"))
    except Exception:
        # Fail closed: an unverified parcel never reaches the prompt.
        logger.warning(
            "mobile_parcel_lookup_failed tenant_id=%s", tenant_id, exc_info=True
        )
        raise _err(404, "invalid_parcel") from None
    finally:
        await client.close()
    if (
        not isinstance(entity, dict)
        or entity.get("id") != parcel_id
        or entity.get("type") not in _PARCEL_TYPES
    ):
        raise _err(404, "invalid_parcel")
    return f'Parcela en contexto: "{_name_of(entity, parcel_id)}" ({parcel_id})'


@router.post("/mobile/turns", status_code=202)
async def create_mobile_turn(
    body: TurnCreate,
    background_tasks: BackgroundTasks,
    user: AuthContext = Depends(get_current_user),
) -> dict:
    settings = get_settings()
    text = body.text.strip()
    if not text:
        raise _err(422, "empty_text")
    if len(text) > settings.mobile_text_max_chars:
        raise _err(422, "text_too_long")

    # A retried submit (same client_turn_id) answers with the existing row
    # before any other gate: it must not 409 on its own still-open turn, spend
    # quota or run again.
    existing = await turns.get_by_client_turn_id(
        user.tenant_id, user.user_id, body.client_turn_id
    )
    if existing is not None:
        return {"turn_id": existing.turn_id, "state": existing.state}

    parcel_context = None
    if body.parcel_id is not None:
        parcel_context = await _parcel_context(user.tenant_id, body.parcel_id)

    await turns.expire_orphans_for_user(
        user.tenant_id, user.user_id, turns.orphan_max_age(settings)
    )
    if await turns.has_open_turn(user.tenant_id, user.user_id):
        raise _err(409, "turn_in_progress")

    blocked = await quota.check_and_count(user.tenant_id, MOBILE_CHANNEL, user.user_id)
    if blocked is not None:
        retry_at = await quota.next_slot(user.tenant_id, MOBILE_CHANNEL, user.user_id)
        raise _err(
            429, "quota_exhausted",
            retry_at=retry_at.astimezone(timezone.utc).isoformat(),
        )

    trace_id = uuid.uuid4().hex
    turn, created = await turns.create_turn(
        user.tenant_id, user.user_id, body.client_turn_id, text,
        body.parcel_id, body.lang, trace_id,
    )
    if created:
        ctx = SessionContext(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            roles=tuple(user.roles),
            channel=MOBILE_CHANNEL,
            channel_user_id=user.user_id,
            trace_id=trace_id,
        )
        background_tasks.add_task(run_mobile_turn, turn.turn_id, ctx, text, parcel_context)
    return {"turn_id": turn.turn_id, "state": turn.state}


def _is_stale(t: turns.MobileTurn) -> bool:
    if t.state not in ("queued", "running"):
        return False
    age = datetime.now(timezone.utc) - t.created_at
    return age > timedelta(seconds=turns.orphan_max_age())


@router.get("/mobile/turns/{turn_id}")
async def get_mobile_turn(
    turn_id: str, user: AuthContext = Depends(get_current_user)
) -> dict:
    turn = await turns.get_turn(user.tenant_id, user.user_id, turn_id)
    if turn is None:
        raise _err(404, "turn_not_found")
    if _is_stale(turn):
        await turns.mark_failed(turn.turn_id, "interrupted", None)
        turn = await turns.get_turn(user.tenant_id, user.user_id, turn_id) or turn
    return _view(turn)


@router.get("/mobile/history")
async def mobile_history(
    limit: int = Query(default=50, ge=1),
    user: AuthContext = Depends(get_current_user),
) -> dict:
    limit = min(limit, get_settings().mobile_history_limit)
    rows = await turns.list_history(user.tenant_id, user.user_id, limit)
    return {"turns": [_view(t) for t in rows]}


def install_error_handlers(app, prefix: str) -> None:
    """Uniform error bodies for the mobile paths only (other routes untouched)."""
    mobile_prefix = f"{prefix}/mobile/"

    def _is_mobile(request: Request) -> bool:
        return request.url.path.startswith(mobile_prefix)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        if not _is_mobile(request):
            from fastapi.exception_handlers import request_validation_exception_handler
            return await request_validation_exception_handler(request, exc)
        fields = [
            ".".join(str(p) for p in e.get("loc", ()) if p != "body")
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "invalid_request", "fields": fields}},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.error(
            "mobile_unhandled_error path=%s", request.url.path, exc_info=exc
        )
        if _is_mobile(request):
            return JSONResponse(status_code=500, content={"detail": {"code": "error"}})
        return PlainTextResponse("Internal Server Error", status_code=500)
