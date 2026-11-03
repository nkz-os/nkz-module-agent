"""
LLM endpoint allowlist (tenant BYOK).

Pure module, no I/O. This is the only barrier between a tenant-supplied
``api_base`` and internal services (SSRF), so parsing is deliberately strict:
anything ambiguous is refused and never widens what is allowed.

Format of the raw value: comma-separated entries, each ``provider`` or
``provider@origin`` where origin = scheme + host + optional port. A bare
``provider`` allows that provider with no ``api_base`` only. No wildcards;
the path of an ``api_base`` is free. No DNS or redirect checks.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
# Any whitespace, control char or backslash inside a URL is a parser-differential
# trick: refuse instead of trying to interpret it.
_HOST_RE = re.compile(r"^[a-z0-9.-]+$")
_FORBIDDEN_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")


def normalize_origin(api_base: str) -> str | None:
    """Return ``scheme://host:port`` (default port explicit, host lowercased).

    None when the value is not an absolute http(s) URL with a host, carries
    userinfo, or has an invalid port.
    """
    if not isinstance(api_base, str):
        return None
    value = api_base.strip()
    if not value or _FORBIDDEN_URL_CHARS.search(value):
        return None
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            return None
        if "@" in parts.netloc or parts.username is not None or parts.password is not None:
            return None
        host = parts.hostname
        if not host:
            return None
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if port < 1 or port > 65535:
        return None
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    elif not _HOST_RE.match(host):
        return None
    return f"{scheme}://{host}:{port}"


@dataclass(frozen=True)
class _Entry:
    provider: str
    origin: str | None


class Allowlist:
    def __init__(self, entries: tuple[_Entry, ...]):
        self._entries = entries

    def allows(self, provider: str, api_base: str | None) -> bool:
        if not isinstance(provider, str):
            return False
        prov = provider.strip().lower()
        if api_base is None or (isinstance(api_base, str) and not api_base.strip()):
            return any(e.provider == prov and e.origin is None for e in self._entries)
        origin = normalize_origin(api_base)
        if origin is None:
            return False
        return any(e.provider == prov and e.origin == origin for e in self._entries)

    def entries(self) -> list[dict]:
        return [{"provider": e.provider, "origin": e.origin} for e in self._entries]


def _parse_entry(item: str) -> _Entry | None:
    provider, sep, origin_raw = item.partition("@")
    provider = provider.strip().lower()
    if not _PROVIDER_RE.match(provider):
        return None
    if not sep:
        return _Entry(provider, None)
    origin_raw = origin_raw.strip()
    origin = normalize_origin(origin_raw)
    if origin is None:
        return None
    # An entry is an origin only: no path, query or fragment.
    rest = urlsplit(origin_raw)
    if rest.path not in ("", "/") or rest.query or rest.fragment:
        return None
    return _Entry(provider, origin)


def parse(raw: str) -> Allowlist:
    entries: list[_Entry] = []
    for index, item in enumerate((raw or "").split(",")):
        item = item.strip()
        if not item:
            continue
        entry = _parse_entry(item)
        if entry is None:
            logger.warning("llm_allowlist_entry_skipped", extra={"position": index})
            continue
        entries.append(entry)
    return Allowlist(tuple(entries))
