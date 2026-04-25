"""Fernet-based credential encryption for Aurora pipeline secrets.

Secrets (GitHub PATs, S3 keys) are encrypted at rest in ir.config_parameter
using Fernet symmetric encryption. The encryption key is auto-generated on
first use and stored in ``aurora.encryption_key``.
"""
from __future__ import annotations

import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "fernet:1:"

ENCRYPTED_PARAMS = frozenset({
    "aurora.s3_access_key",
    "aurora.s3_secret_key",
})

_key_cache_lock = threading.Lock()
_cached_fernet_key: bytes | None = None
_cached_fernet_key_raw: bytes | None = None


def _get_or_create_key(ICP) -> bytes:
    global _cached_fernet_key
    env_key = os.environ.get("AURORA_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    with _key_cache_lock:
        if _cached_fernet_key is not None:
            return _cached_fernet_key
    key_str = ICP.get_param("aurora.encryption_key", "")
    if key_str:
        _logger.error(
            "Aurora: encryption key loaded from database — NOT SAFE FOR PRODUCTION. "
            "Set AURORA_ENCRYPTION_KEY environment variable."
        )
        result = key_str.encode()
        with _key_cache_lock:
            _cached_fernet_key = result
        return result
    key = Fernet.generate_key()
    ICP.set_param("aurora.encryption_key", key.decode())
    _logger.warning(
        "Aurora: generated new Fernet encryption key and stored in database. "
        "For production, set AURORA_ENCRYPTION_KEY environment variable."
    )
    with _key_cache_lock:
        _cached_fernet_key = key
    return key


def _get_previous_key(ICP) -> bytes | None:
    env_prev = os.environ.get("AURORA_ENCRYPTION_KEY_PREVIOUS", "").strip()
    if env_prev:
        return env_prev.encode()
    prev_str = ICP.get_param("aurora.encryption_key_previous", "")
    if prev_str:
        return prev_str.encode()
    return None


def _make_fernet(ICP) -> Fernet | MultiFernet:
    current = _get_or_create_key(ICP)
    previous = _get_previous_key(ICP)
    if previous:
        return MultiFernet([Fernet(current), Fernet(previous)])
    return Fernet(current)


def _get_or_create_key_raw(cr) -> bytes:
    global _cached_fernet_key_raw
    env_key = os.environ.get("AURORA_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    with _key_cache_lock:
        if _cached_fernet_key_raw is not None:
            return _cached_fernet_key_raw
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        ("aurora.encryption_key",),
    )
    row = cr.fetchone()
    if row and row[0]:
        _logger.error(
            "Aurora: encryption key loaded from database (background thread) — NOT SAFE FOR PRODUCTION. "
            "Set AURORA_ENCRYPTION_KEY environment variable."
        )
        result = row[0].encode()
        with _key_cache_lock:
            _cached_fernet_key_raw = result
        return result
    key = Fernet.generate_key()
    cr.execute(
        """
        INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
        VALUES (%s, %s, 1, 1, now() at time zone 'UTC', now() at time zone 'UTC')
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = now() at time zone 'UTC'
        """,
        ("aurora.encryption_key", key.decode()),
    )
    cr.commit()
    _logger.info("Aurora: generated new Fernet encryption key (raw SQL).")
    with _key_cache_lock:
        _cached_fernet_key_raw = key
    return key


def _get_previous_key_raw(cr) -> bytes | None:
    env_prev = os.environ.get("AURORA_ENCRYPTION_KEY_PREVIOUS", "").strip()
    if env_prev:
        return env_prev.encode()
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        ("aurora.encryption_key_previous",),
    )
    row = cr.fetchone()
    if row and row[0]:
        return row[0].encode()
    return None


def _make_fernet_raw(cr) -> Fernet | MultiFernet:
    current = _get_or_create_key_raw(cr)
    previous = _get_previous_key_raw(cr)
    if previous:
        return MultiFernet([Fernet(current), Fernet(previous)])
    return Fernet(current)


def encrypt_value(ICP, plaintext: str) -> str:
    """Encrypt a plaintext string. Returns prefixed ciphertext."""
    if not plaintext:
        return ""
    f = _make_fernet(ICP)
    encrypted = f.encrypt(plaintext.encode()).decode()
    return f"{_ENCRYPTED_PREFIX}{encrypted}"


def decrypt_value(ICP, stored: str) -> str:
    """Decrypt a stored value. Handles plaintext (migration) gracefully."""
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
            "Aurora: failed to decrypt config value. "
            "Key may have changed or data is corrupted. Returning empty string."
        )
        return ""


def decrypt_value_raw(cr, stored: str) -> str:
    """Decrypt using raw cursor (for background threads)."""
    if not stored:
        return ""
    if not stored.startswith(_ENCRYPTED_PREFIX):
        return stored
    token = stored[len(_ENCRYPTED_PREFIX):]
    f = _make_fernet_raw(cr)
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        _logger.error(
            "Aurora: failed to decrypt config value (raw). "
            "Key may have changed or data is corrupted. Returning empty string."
        )
        return ""



def get_encrypted_param(env, key: str, default: str = "") -> str:
    """Read and decrypt a value from ir.config_parameter."""
    ICP = env["ir.config_parameter"].sudo()
    stored = ICP.get_param(key, default)
    if key in ENCRYPTED_PARAMS:
        return decrypt_value(ICP, stored)
    return stored


def get_encrypted_param_raw(cr, key: str, default: str = "") -> str:
    """Read and decrypt using raw cursor (for background threads)."""
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        (key,),
    )
    row = cr.fetchone()
    stored = row[0] if row else default
    if key in ENCRYPTED_PARAMS:
        return decrypt_value_raw(cr, stored)
    return stored or default
