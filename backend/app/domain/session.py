"""Session identity. Built once at the edge, immutable thereafter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionContext:
    """The single source of tenant_id for a turn.

    Never derived from model output, never accepted as a tool argument.
    """

    tenant_id: str
    user_id: str
    roles: tuple[str, ...]
    channel: str
    channel_user_id: str
    trace_id: str
