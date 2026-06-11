"""Fernet encryption-at-rest for Iris credentials stored in ir.config_parameter.

Adapted from ``crowley/models/credential_manager.py``. Secrets listed in
``ENCRYPTED_PARAMS`` are stored as ``fernet:1:<token>`` and transparently
decrypted on read. The encryption key resolution order:

1. ``IRIS_ENCRYPTION_KEY`` environment variable (production),
2. ``iris.encryption_key`` ICP value (dev only — logged as unsafe),
3. auto-generated Fernet key persisted to the ICP (dev bootstrap).

Key rotation: set ``IRIS_ENCRYPTION_KEY`` to the new key and
``IRIS_ENCRYPTION_KEY_PREVIOUS`` (or ICP ``iris.encryption_key_previous``)
to the old one; ``MultiFernet`` decrypts values written under either key.
Nothing in this module reads any secret at import/registry-load time, so a
clean install with no key configured never fails here.
"""

from __future__ import annotations

import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "fernet:1:"

#: ICP keys whose values are Fernet-encrypted at rest.
ENCRYPTED_PARAMS = frozenset({
    "iris.openrouter_api_key",
})

_key_cache_lock = threading.Lock()
# DB-derived keys cached per database name — a multi-db deployment must
# never reuse the first database's key for the others.
_cached_fernet_keys: dict[str, bytes] = {}


def _get_or_create_key(ICP) -> bytes:
    """Resolve the current Fernet key (env var > ICP > auto-generate)."""
    env_key = os.environ.get("IRIS_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    dbname = ICP.env.cr.dbname
    with _key_cache_lock:
        cached = _cached_fernet_keys.get(dbname)
    if cached is not None:
        return cached
    key_str = ICP.get_param("iris.encryption_key", "")
    if key_str:
        _logger.error(
            "Iris: encryption key loaded from database — NOT SAFE FOR PRODUCTION. "
            "Set IRIS_ENCRYPTION_KEY environment variable."
        )
        result = key_str.encode()
        with _key_cache_lock:
            _cached_fernet_keys[dbname] = result
        return result
    key = Fernet.generate_key()
    ICP.set_param("iris.encryption_key", key.decode())
    _logger.warning(
        "Iris: generated new Fernet encryption key and stored in database. "
        "For production, set IRIS_ENCRYPTION_KEY environment variable."
    )
    with _key_cache_lock:
        _cached_fernet_keys[dbname] = key
    return key


def _get_previous_key(ICP) -> bytes | None:
    """Resolve the previous (rotation) key, if any."""
    env_prev = os.environ.get("IRIS_ENCRYPTION_KEY_PREVIOUS", "").strip()
    if env_prev:
        return env_prev.encode()
    prev_str = ICP.get_param("iris.encryption_key_previous", "")
    if prev_str:
        return prev_str.encode()
    return None


def _make_fernet(ICP) -> Fernet | MultiFernet:
    """Build the Fernet (or MultiFernet, during rotation) cipher."""
    current = _get_or_create_key(ICP)
    previous = _get_previous_key(ICP)
    if previous:
        return MultiFernet([Fernet(current), Fernet(previous)])
    return Fernet(current)


def encrypt_value(ICP, plaintext: str) -> str:
    """Encrypt ``plaintext`` for storage, prefixed with the scheme marker."""
    if not plaintext:
        return ""
    f = _make_fernet(ICP)
    encrypted = f.encrypt(plaintext.encode()).decode()
    return f"{_ENCRYPTED_PREFIX}{encrypted}"


def decrypt_value(ICP, stored: str) -> str:
    """Decrypt a stored value; legacy plaintext values pass through as-is."""
    if not stored:
        return ""
    if not stored.startswith(_ENCRYPTED_PREFIX):
        return stored
    token = stored[len(_ENCRYPTED_PREFIX):]
    f = _make_fernet(ICP)
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        _logger.error(
            "Iris: failed to decrypt config value. "
            "Key may have changed or data is corrupted. Returning empty string."
        )
        return ""


def get_encrypted_param(env, key: str, default: str = "") -> str:
    """Read an ICP value, decrypting it when ``key`` is in ENCRYPTED_PARAMS."""
    ICP = env["ir.config_parameter"].sudo()
    stored = ICP.get_param(key, default)
    if key in ENCRYPTED_PARAMS:
        decrypted = decrypt_value(ICP, stored)
        if not decrypted and stored:
            _logger.warning(
                "Iris: encrypted param '%s' is set but decryption returned empty. "
                "Check IRIS_ENCRYPTION_KEY matches the key used to encrypt.",
                key,
            )
        return decrypted
    return stored


def set_encrypted_param(env, key: str, plaintext: str) -> None:
    """Write an ICP value, encrypting it when ``key`` is in ENCRYPTED_PARAMS."""
    ICP = env["ir.config_parameter"].sudo()
    if key in ENCRYPTED_PARAMS:
        stored = encrypt_value(ICP, plaintext) if plaintext else ""
    else:
        stored = plaintext or ""
    ICP.set_param(key, stored)


def get_openrouter_api_key(env) -> str:
    """Decrypted OpenRouter API key, or empty string when unset."""
    return get_encrypted_param(env, "iris.openrouter_api_key")
