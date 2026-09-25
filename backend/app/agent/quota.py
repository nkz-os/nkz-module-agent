"""Spend caps for a publicly reachable endpoint backed by a paid model.

The platform's rule for feature quotas is fail-open, and that is right for
features. This is money: a public webhook plus a per-token model is a pump, so
the caps here are finite and enforced. Documented deviation, spec §5.5.

Counts come from the audit table, which already records every turn with a
timestamp — a second counter would be a second thing to keep correct.

Design decisions (deliberate, documented for review):

* Account hourly budget is scoped to ``(channel, channel_user_id)`` with NO
  tenant filter. A human gets one rolling hourly budget even when talking to
  two tenants: the resource being protected (the human's attention/pocket, and
  the model behind it) belongs to the human, not to a tenant pairing. The
  tenant daily cap is the tenant-scoped protection; both exist.
* Blocked turns are never inserted, so they never count. If they counted, a
  spammer who is already blocked would keep inflating the counters with
  messages that never reached the model, keeping the tenant locked out far
  beyond the rolling window of real usage.
* Precedence when both caps are hit simultaneously is deterministic: the
  account check runs first, so the reported reason is always
  ``"account_hourly"``. Guarded by test_both_caps_reached_reports_account_hourly.
* Known limitation (v1): TOCTOU race. Two concurrent webhooks can both pass
  the check before either one audits its turn, so a burst slightly above the
  cap can slip through. Conservative caps contain the blast radius; a
  serialized check-and-audit path is Task 9 material if needed.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.db import get_pool

logger = logging.getLogger(__name__)


async def check_and_count(
    tenant_id: str, channel: str, channel_user_id: str
) -> str | None:
    """Return None to allow, or the name of the cap that blocked the turn."""
    settings = get_settings()
    pool = await get_pool()

    per_account = await pool.fetchval(
        """
        SELECT count(*) FROM agent_turn_audit
         WHERE channel = $1 AND channel_user_id = $2
           AND created_at > now() - interval '1 hour'
        """,
        channel, channel_user_id,
    )
    if per_account >= settings.max_turns_per_account_hour:
        logger.warning(
            "quota_blocked reason=account_hourly channel=%s", channel
        )
        return "account_hourly"

    per_tenant = await pool.fetchval(
        """
        SELECT count(*) FROM agent_turn_audit
         WHERE tenant_id = $1 AND created_at > now() - interval '1 day'
        """,
        tenant_id,
    )
    if per_tenant >= settings.max_turns_per_tenant_day:
        logger.warning(
            "quota_blocked reason=tenant_daily tenant_id=%s", tenant_id
        )
        return "tenant_daily"

    return None
