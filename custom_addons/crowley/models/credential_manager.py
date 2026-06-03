from __future__ import annotations

import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "fernet:1:"

ENCRYPTED_PARAMS = frozenset({
    "crowley.openrouter_api_key",
    "crowley.aws_access_key",
    "crowley.aws_secret_key",
    "crowley.webhook_secret",
})

_key_cache_lock = threading.Lock()
_cached_fernet_key: bytes | None = None
_cached_fernet_key_raw: bytes | None = None


def _get_or_create_key(ICP) -> bytes:
    global _cached_fernet_key
    env_key = os.environ.get("CROWLEY_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    with _key_cache_lock:
        if _cached_fernet_key is not None:
            return _cached_fernet_key
    key_str = ICP.get_param("crowley.encryption_key", "")
    if key_str:
        _logger.error(
            "Crowley: encryption key loaded from database — NOT SAFE FOR PRODUCTION. "
            "Set CROWLEY_ENCRYPTION_KEY environment variable."
        )
        result = key_str.encode()
        with _key_cache_lock:
            _cached_fernet_key = result
        return result
    key = Fernet.generate_key()
    ICP.set_param("crowley.encryption_key", key.decode())
    _logger.warning(
        "Crowley: generated new Fernet encryption key and stored in database. "
        "For production, set CROWLEY_ENCRYPTION_KEY environment variable."
    )
    with _key_cache_lock:
        _cached_fernet_key = key
    return key


def _get_previous_key(ICP) -> bytes | None:
    env_prev = os.environ.get("CROWLEY_ENCRYPTION_KEY_PREVIOUS", "").strip()
    if env_prev:
        return env_prev.encode()
    prev_str = ICP.get_param("crowley.encryption_key_previous", "")
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
    env_key = os.environ.get("CROWLEY_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    with _key_cache_lock:
        if _cached_fernet_key_raw is not None:
            return _cached_fernet_key_raw
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        ("crowley.encryption_key",),
    )
    row = cr.fetchone()
    if row and row[0]:
        _logger.error(
            "Crowley: encryption key loaded from database (background thread) — "
            "NOT SAFE FOR PRODUCTION. Set CROWLEY_ENCRYPTION_KEY environment variable."
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
        ("crowley.encryption_key", key.decode()),
    )
    cr.commit()
    _logger.info("Crowley: generated new Fernet encryption key (raw SQL).")
    with _key_cache_lock:
        _cached_fernet_key_raw = key
    return key


def _get_previous_key_raw(cr) -> bytes | None:
    env_prev = os.environ.get("CROWLEY_ENCRYPTION_KEY_PREVIOUS", "").strip()
    if env_prev:
        return env_prev.encode()
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        ("crowley.encryption_key_previous",),
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
            "Crowley: failed to decrypt config value. "
            "Key may have changed or data is corrupted. Returning empty string."
        )
        return ""


def decrypt_value_raw(cr, stored: str) -> str:
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
            "Crowley: failed to decrypt config value (raw). "
            "Key may have changed or data is corrupted. Returning empty string."
        )
        return ""


def get_encrypted_param(env, key: str, default: str = "") -> str:
    ICP = env["ir.config_parameter"].sudo()
    stored = ICP.get_param(key, default)
    if key in ENCRYPTED_PARAMS:
        decrypted = decrypt_value(ICP, stored)
        if not decrypted and stored:
            _logger.warning(
                "Crowley: encrypted param '%s' is set but decryption returned empty. "
                "Check CROWLEY_ENCRYPTION_KEY matches the key used to encrypt.",
                key,
            )
        return decrypted
    return stored


def get_encrypted_param_raw(cr, key: str, default: str = "") -> str:
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        (key,),
    )
    row = cr.fetchone()
    stored = row[0] if row else default
    if key in ENCRYPTED_PARAMS:
        return decrypt_value_raw(cr, stored)
    return stored or default


def set_encrypted_param(env, key: str, plaintext: str) -> None:
    ICP = env["ir.config_parameter"].sudo()
    if key in ENCRYPTED_PARAMS:
        stored = encrypt_value(ICP, plaintext) if plaintext else ""
    else:
        stored = plaintext or ""
    ICP.set_param(key, stored)


def get_openrouter_api_key(env):
    return get_encrypted_param(env, "crowley.openrouter_api_key")


def get_openrouter_api_key_raw(cr):
    return get_encrypted_param_raw(cr, "crowley.openrouter_api_key")


def get_aws_access_key(env):
    return get_encrypted_param(env, "crowley.aws_access_key")


def get_aws_access_key_raw(cr):
    return get_encrypted_param_raw(cr, "crowley.aws_access_key")


def get_aws_secret_key(env):
    return get_encrypted_param(env, "crowley.aws_secret_key")


def get_aws_secret_key_raw(cr):
    return get_encrypted_param_raw(cr, "crowley.aws_secret_key")


def get_webhook_secret(env):
    return get_encrypted_param(env, "crowley.webhook_secret")


def get_webhook_secret_raw(cr):
    return get_encrypted_param_raw(cr, "crowley.webhook_secret")
