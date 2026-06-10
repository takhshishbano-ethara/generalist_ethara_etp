from __future__ import annotations

import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "fernet:1:"

ENCRYPTED_PARAMS = frozenset({
    "i2i.openrouter_api_key",
})

_key_cache_lock = threading.Lock()
_cached_fernet_key: bytes | None = None


def _get_or_create_key(ICP) -> bytes:
    global _cached_fernet_key
    env_key = os.environ.get("I2I_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    with _key_cache_lock:
        if _cached_fernet_key is not None:
            return _cached_fernet_key
    key_str = ICP.get_param("i2i.encryption_key", "")
    if key_str:
        _logger.error(
            "I2I: encryption key loaded from database \u2014 NOT SAFE FOR "
            "PRODUCTION. Set I2I_ENCRYPTION_KEY environment variable."
        )
        result = key_str.encode()
        with _key_cache_lock:
            _cached_fernet_key = result
        return result
    key = Fernet.generate_key()
    ICP.set_param("i2i.encryption_key", key.decode())
    _logger.warning(
        "I2I: generated new Fernet encryption key and stored in database. "
        "For production, set I2I_ENCRYPTION_KEY environment variable."
    )
    with _key_cache_lock:
        _cached_fernet_key = key
    return key


def _get_previous_key(ICP) -> bytes | None:
    env_prev = os.environ.get("I2I_ENCRYPTION_KEY_PREVIOUS", "").strip()
    if env_prev:
        return env_prev.encode()
    prev_str = ICP.get_param("i2i.encryption_key_previous", "")
    if prev_str:
        return prev_str.encode()
    return None


def _make_fernet(ICP) -> Fernet | MultiFernet:
    current = _get_or_create_key(ICP)
    previous = _get_previous_key(ICP)
    if previous:
        return MultiFernet([Fernet(current), Fernet(previous)])
    return Fernet(current)


def encrypt_value(ICP, plaintext: str) -> str:
    if not plaintext:
        return ""
    f = _make_fernet(ICP)
    token = f.encrypt(plaintext.encode()).decode()
    return f"{_ENCRYPTED_PREFIX}{token}"


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
            "I2I: failed to decrypt config value. Key may have changed or "
            "data is corrupted. Returning empty string."
        )
        return ""


def get_encrypted_param(env, key: str, default: str = "") -> str:
    ICP = env["ir.config_parameter"].sudo()
    stored = ICP.get_param(key, default)
    if key in ENCRYPTED_PARAMS:
        return decrypt_value(ICP, stored)
    return stored


def set_encrypted_param(env, key: str, plaintext: str) -> None:
    ICP = env["ir.config_parameter"].sudo()
    if key in ENCRYPTED_PARAMS:
        stored = encrypt_value(ICP, plaintext) if plaintext else ""
    else:
        stored = plaintext or ""
    ICP.set_param(key, stored)


def get_openrouter_api_key(env) -> str:
    return get_encrypted_param(env, "i2i.openrouter_api_key")
