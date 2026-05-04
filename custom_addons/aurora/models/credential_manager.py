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
    "aurora.webhook_secret",
    "aurora.github_registry_write_token",
})

_key_cache_lock = threading.Lock()
# Keyed by DB name to support multi-database Odoo deployments.
_cached_fernet_keys: dict[str, bytes] = {}
_cached_fernet_keys_raw: dict[str, bytes] = {}


def _get_or_create_key(ICP) -> bytes:
    env_key = os.environ.get("AURORA_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    db_name = ICP.env.cr.dbname
    with _key_cache_lock:
        if db_name in _cached_fernet_keys:
            return _cached_fernet_keys[db_name]
    key_str = ICP.get_param("aurora.encryption_key", "")
    if key_str:
        _logger.critical(
            "Aurora: encryption key loaded from database. This is INSECURE — anyone with "
            "DB read access can decrypt every stored secret. Copy the current value of "
            "ir_config_parameter['aurora.encryption_key'] into the AURORA_ENCRYPTION_KEY "
            "environment variable on every Odoo worker, restart, then manually delete the "
            "DB row. Continuing with the DB-stored key for now to avoid breaking ciphertexts."
        )
        result = key_str.encode()
        with _key_cache_lock:
            _cached_fernet_keys[db_name] = result
        return result
    raise RuntimeError(
        "Aurora encryption key is not available. Set the AURORA_ENCRYPTION_KEY environment "
        "variable on every Odoo worker (a Fernet key string, 44 bytes base64). "
        "Auto-generation into the database has been disabled because it stores the key "
        "next to the ciphertexts it protects — anyone with a DB dump would own every secret. "
        "If this is a fresh install, generate one with: "
        "`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`"
    )


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
    env_key = os.environ.get("AURORA_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    db_name = cr.dbname if hasattr(cr, "dbname") else "default"
    with _key_cache_lock:
        if db_name in _cached_fernet_keys_raw:
            return _cached_fernet_keys_raw[db_name]
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        ("aurora.encryption_key",),
    )
    row = cr.fetchone()
    if row and row[0]:
        _logger.critical(
            "Aurora: encryption key loaded from database (background thread). This is INSECURE. "
            "Copy ir_config_parameter['aurora.encryption_key'] into the AURORA_ENCRYPTION_KEY "
            "environment variable, restart Odoo workers, then delete the DB row."
        )
        result = row[0].encode()
        with _key_cache_lock:
            _cached_fernet_keys_raw[db_name] = result
        return result
    raise RuntimeError(
        "Aurora encryption key is not available in background thread. Set AURORA_ENCRYPTION_KEY "
        "environment variable on every Odoo worker. Auto-generation into the database has been "
        "disabled for security reasons."
    )


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
