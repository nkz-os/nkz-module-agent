"""Encryption of tenant LLM API keys (Fernet, rotatable via MultiFernet).

Master keys come from LLM_KEY_ENCRYPTION_KEYS (comma-separated, first one
encrypts, all decrypt). They are read from settings on every call, with no
module-level cache, so a settings reload takes effect immediately.

Fail-closed: without a usable master key nothing is ever stored in clear;
``available()`` is False and ``encrypt``/``decrypt`` raise ``KeyUnreadable``.
No message raised here ever contains plaintext or ciphertext.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings


class KeyUnreadable(Exception):
    """The key cannot be encrypted/decrypted (no master key, wrong key, corrupt)."""


def _fernets() -> list[Fernet]:
    raw = get_settings().llm_key_encryption_keys
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    try:
        return [Fernet(p.encode()) for p in parts]
    except (ValueError, TypeError):
        # Malformed master key: treat as no keys at all (fail-closed).
        return []


def available() -> bool:
    """True when at least one valid master key is configured (and all parse)."""
    raw = get_settings().llm_key_encryption_keys
    parts = [p for p in (x.strip() for x in raw.split(",")) if p]
    return bool(parts) and len(_fernets()) == len(parts)


def _multi() -> MultiFernet:
    if not available():
        raise KeyUnreadable("no usable LLM key encryption key configured")
    return MultiFernet(_fernets())


def encrypt(plaintext: str) -> bytes:
    return _multi().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    try:
        return _multi().decrypt(bytes(token)).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        raise KeyUnreadable("stored key cannot be decrypted") from None


def reencrypt(token: bytes) -> bytes:
    """Re-encrypt under the first master key (reads with any configured key)."""
    try:
        return _multi().rotate(bytes(token))
    except (InvalidToken, ValueError, TypeError):
        raise KeyUnreadable("stored key cannot be decrypted") from None


def uses_primary_key(token: bytes) -> bool:
    """True when the first (encrypting) master key alone can read the token."""
    if not available():
        return False
    try:
        _fernets()[0].decrypt(bytes(token))
        return True
    except (InvalidToken, ValueError, TypeError):
        return False


def last4(plaintext: str) -> str:
    return plaintext.strip()[-4:]
