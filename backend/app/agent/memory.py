"""24-hour conversational memory, keyed by the exact session identity.

One row per ``(tenant_id, channel, channel_user_id)``. The tenant is part of
the primary key and of every statement here: isolation is the primary key
plus the discipline that every query qualifies all three columns, not
row-level security (the table has none — it is private by construction and is
never read except through its full key).

Two properties that are easy to lose silently:

* Loading is bounded by the turn's own token budget (derived by the caller),
  so memory can shrink a prompt but never grow it. The newest messages fit
  first; anything older that does not fit is dropped and a single ``…`` system
  marker stands in for the gap.
* Purge is opportunistic, not a scheduler. A ``DELETE`` on ``updated_at`` is
  a sequential scan of the whole table, so running it on every save would add
  a second table scan to a hot path. It runs every ``_PURGE_EVERY_N_SAVES``
  saves instead, which bounds the amortised cost without introducing a cron.
  The inbound-update dedupe's ``purge_expired()`` rides the same cadence.
"""

from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.db import get_pool
from app.dedupe import purge_expired
from app.domain.session import SessionContext

logger = logging.getLogger(__name__)

# Inserted at the head of a truncated load so the model can see that context
# was dropped, rather than reading two disconnected turns as continuous.
_MARKER = {"role": "system", "content": "…"}
_MARKER_CHARS = len(_MARKER["content"])

# Opportunistic purge cadence (see module docstring). The counter is a plain
# module int: worst case under concurrency is one extra purge, never a skip
# that matters for correctness — the load path already treats expired rows as
# empty, so purge is only storage reclamation.
_PURGE_EVERY_N_SAVES = 20

_save_counter = 0


def _message_chars(message: dict) -> int:
    """Characters a message contributes to the loaded-context budget."""
    return len(str(message.get("content") or ""))


def _total_chars(messages: list[dict]) -> int:
    return sum(_message_chars(m) for m in messages)


def _trim_to_budget(messages: list[dict], budget_chars: int) -> list[dict]:
    """Keep the newest messages that fit, prefixing a marker if any dropped.

    Iterates from the end so the most recent messages are kept first; the
    marker's own cost is reserved up front so ``total <= budget_chars`` holds
    even when a cut happens.
    """
    if budget_chars <= 0:
        return []
    if _total_chars(messages) <= budget_chars:
        return list(messages)

    limit = budget_chars - _MARKER_CHARS
    kept: list[dict] = []
    used = 0
    for message in reversed(messages):
        cost = _message_chars(message)
        if used + cost > limit:
            break
        kept.append(message)
        used += cost
    kept.reverse()
    return [_MARKER, *kept]


def _essential(messages: list[dict]) -> list[dict]:
    """Keep only what helps the next turn: the user's message and the final
    assistant answer. Intermediate ``role: tool`` results and the assistant's
    ``tool_calls`` turns are noise, not context."""
    essential: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "user" or (role == "assistant" and not message.get("tool_calls")):
            essential.append(message)
    return essential


async def load(ctx: SessionContext, budget_chars: int) -> list[dict]:
    """Load the prior thread by its exact PK (tenant_id, channel, channel_user_id).

    TTL 24 h (MEMORY_TTL_HOURS): a row with updated_at < now()-TTL loads as
    empty. Trims from the END — the most recent messages fit first; when a cut
    happens a single system "…" marker is prepended to the loaded context.
    Memory never grows the budget: budget_chars is derived from the turn.
    """
    settings = get_settings()
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT messages
          FROM agent_conversation_state
         WHERE tenant_id = $1 AND channel = $2 AND channel_user_id = $3
           AND updated_at >= now() - ($4 || ' hours')::interval
        """,
        ctx.tenant_id,
        ctx.channel,
        ctx.channel_user_id,
        str(settings.memory_ttl_hours),
    )
    if row is None:
        return []
    raw = row["messages"]
    messages = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(messages, list):
        # A malformed row (manual edit, future schema drift) must not
        # permanently break this account's memory: treat it as empty. The
        # next save overwrites the row with a well-formed thread.
        logger.warning("memory_row_malformed tenant_id=%s channel=%s",
                       ctx.tenant_id, ctx.channel)
        return []
    messages = [m for m in messages if isinstance(m, dict)]
    return _trim_to_budget(messages, budget_chars)


async def _purge_expired_rows() -> None:
    """DELETE expired threads on a connection of its own (see save)."""
    settings = get_settings()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_conversation_state "
            "WHERE updated_at < now() - ($1 || ' hours')::interval",
            str(settings.memory_ttl_hours),
        )


def _should_purge() -> bool:
    global _save_counter
    _save_counter += 1
    return _save_counter % _PURGE_EVERY_N_SAVES == 0


async def save(ctx: SessionContext, messages: list[dict], budget_chars: int) -> None:
    """Append this turn's essentials to the stored thread, trimmed to the
    same budget the load path uses.

    Read-modify-write inside one transaction (SELECT the stored messages,
    append _essential(messages), trim to budget_chars, upsert). The thread
    grows turn over turn within the TTL window and the trim is what keeps it
    bounded — that is the whole point of a rolling memory. Concurrent turns
    of the same account are last-write-wins: acceptable for conversational
    context (each turn is self-contained; the loser's exchange simply is not
    remembered), and the alternative — merging under contention — buys
    nothing for a best-effort context.

    Opportunistic purge runs on its own transaction after the commit, gated
    by a module counter (every N saves) so a hot turn does not pay a full
    table scan every time — and so a failing purge can never roll back the
    thread that was just saved.
    """
    essential = _essential(messages)
    settings = get_settings()
    pool = await get_pool()
    purge = _should_purge()

    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT messages FROM agent_conversation_state
             WHERE tenant_id = $1 AND channel = $2 AND channel_user_id = $3
               AND updated_at >= now() - ($4 || ' hours')::interval
            """,
            ctx.tenant_id, ctx.channel, ctx.channel_user_id,
            str(settings.memory_ttl_hours),
        )
        stored: list = []
        if row is not None:
            raw = row["messages"]
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, list):
                stored = [m for m in parsed if isinstance(m, dict)]
        merged = _trim_to_budget(stored + essential, budget_chars)
        await conn.execute(
            """
            INSERT INTO agent_conversation_state
                   (tenant_id, channel, channel_user_id, messages, updated_at)
            VALUES ($1, $2, $3, $4::jsonb, now())
            ON CONFLICT (tenant_id, channel, channel_user_id)
            DO UPDATE SET messages = EXCLUDED.messages, updated_at = now()
            """,
            ctx.tenant_id,
            ctx.channel,
            ctx.channel_user_id,
            json.dumps(merged),
        )

    if purge:
        # Reclaiming space is best-effort: the rows an expired thread
        # occupies are invisible to load() anyway (the TTL predicate), so a
        # skipped or failed purge costs storage, never correctness — and
        # (running after the commit, on its own connection) it must never
        # cost the save that just landed.
        try:
            await _purge_expired_rows()
        except Exception:
            logger.warning(
                "memory_purge_failed tenant_id=%s channel=%s",
                ctx.tenant_id,
                ctx.channel,
                exc_info=True,
            )
        # The dedupe table rides the same opportunistic cadence. It already
        # has its own tested purge_expired(); reusing it keeps the retention
        # window in one place. A failure here must never break the turn (and
        # the memory upsert above already committed).
        try:
            await purge_expired()
        except Exception:
            logger.warning(
                "dedupe_purge_failed tenant_id=%s channel=%s",
                ctx.tenant_id,
                ctx.channel,
                exc_info=True,
            )
