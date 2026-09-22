"""Public channel webhook.

This is the only route that does NOT arrive through the api-gateway, so it
carries no X-Tenant-ID / X-User-ID headers and cannot use require_auth(). Its
sole authentication is a shared secret registered with the channel when the
webhook was set up. Everything downstream — which tenant this message belongs
to — hangs off the sender identifier inside the body, so a forged request
that slips past this check breaks tenant isolation entirely. Compare the
secret with hmac.compare_digest, never `==`, and never log the request body:
an unauthenticated caller must not be able to write chosen content into logs.
"""

from __future__ import annotations

import hmac
import logging
import uuid

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

from app.channels.telegram import TelegramAdapter
from app.config import get_settings
from app.dedupe import claim_update
from app.handlers import handle_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])
adapter = TelegramAdapter()


def _authorised(provided: str | None) -> bool:
    """Constant-time comparison. An unset secret rejects everything (fail closed).

    Compared as utf-8 bytes, not str: hmac.compare_digest raises TypeError
    when either operand is a non-ASCII str, so a raw non-ASCII header byte
    would otherwise 500 (harmless from an attacker's side — it still fails
    closed — but a non-ASCII TELEGRAM_WEBHOOK_SECRET would then 500 every
    legitimate delivery forever, and the platform retries anything that
    isn't 2xx). Encoding both sides removes the restriction entirely; the
    except is defense-in-depth so any future comparison error still resolves
    to "rejected", never to an unhandled 500.

    PROHIBITED: do not add a length precheck (e.g. `if len(provided) !=
    len(expected): return False`) before the compare_digest call, even as
    an "optimisation" or a guard against calling compare_digest on mismatched
    lengths. compare_digest is constant-time specifically so that a
    mismatched length or byte position leaks nothing through timing; a
    precheck like that reintroduces exactly the side channel it exists to
    close (early return on impossible-length input observable via response
    latency, byte-position leakage on longer-than-expected input).
    """
    expected = get_settings().telegram_webhook_secret
    if not expected:
        logger.critical("webhook_secret_unset — rejecting all inbound updates")
        return False
    try:
        return hmac.compare_digest(
            (provided or "").encode("utf-8"), expected.encode("utf-8")
        )
    except (TypeError, ValueError):
        return False


async def send_reply(payload: dict) -> None:
    token = get_settings().telegram_bot_token
    if not token:
        logger.critical("bot_token_unset — cannot deliver reply")
        return
    async with httpx.AsyncClient(timeout=10) as http_client:
        await http_client.post(
            f"https://api.telegram.org/bot{token}/sendMessage", json=payload
        )


async def process_update(raw: dict) -> None:
    """Handle one update. Runs after the response has already been returned.

    Wrapped in a broad except: claim_update() has already committed by the
    time handle_message() could raise, so the update is already marked
    processed and the platform will never retry it — on a /start turn the
    link token may already be burned too. Losing this exception silently
    would mean losing the turn with no record it ever happened. There is no
    ASGI caller left to see a re-raise (the response is already gone), so
    this logs with the identifiers needed to find the lost turn and stops.
    """
    trace_id = str(uuid.uuid4())
    msg = adapter.parse(raw)
    if msg is None:
        return

    try:
        if not await claim_update(msg.idempotency_key):
            logger.info("update_already_processed key=%s", msg.idempotency_key)
            return

        logger.info(
            "turn_start channel=%s key=%s trace_id=%s",
            msg.channel, msg.idempotency_key, trace_id,
        )
        reply = await handle_message(msg, trace_id)
        await send_reply(adapter.render(reply, chat_id=msg.channel_user_id))
    except Exception:
        logger.exception(
            "turn_failed channel=%s key=%s trace_id=%s",
            msg.channel, msg.idempotency_key, trace_id,
        )


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """Acknowledge fast, process out of band.

    The platform retries anything it does not see acknowledged promptly, and
    a retry would be a second full execution of the turn.
    """
    if not _authorised(x_telegram_bot_api_secret_token):
        # Deliberately no body/payload in this log line: an unauthenticated
        # caller must not be able to write content of their choosing into
        # our logs.
        logger.warning("webhook_rejected path=%s", request.url.path)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        raw = await request.json()
    except ValueError:
        # Not valid JSON at all. Acknowledge anyway: a non-2xx here earns a
        # retry loop delivering the same malformed body forever, and this
        # caller already holds the shared secret so there is no isolation
        # question left — but its content is still not something we log.
        logger.warning("webhook_malformed_body path=%s", request.url.path)
        return {"ok": True}

    if not isinstance(raw, dict):
        # Valid JSON, but not an update object (list, string, number, null).
        # Same fail-safe outcome as a kind the adapter cannot parse: ack, do
        # nothing, no retry storm — and still no body content in the log.
        logger.warning("webhook_malformed_body path=%s", request.url.path)
        return {"ok": True}

    background.add_task(process_update, raw)
    return {"ok": True}
