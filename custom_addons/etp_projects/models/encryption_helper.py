from __future__ import annotations

import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "fernet:1:"

_ENV_KEY_CURRENT = "ETP_ENCRYPTION_KEY"
_ENV_KEY_PREVIOUS = "ETP_ENCRYPTION_KEY_PREVIOUS"
_ICP_KEY_CURRENT = "etp_projects.encryption_key"
_ICP_KEY_PREVIOUS = "etp_projects.encryption_key_previous"

_key_cache_lock = threading.Lock()
_cached_current_key: bytes | None = None


def _load_current_key(ICP) -> bytes:
    global _cached_current_key

    env_key = os.environ.get(_ENV_KEY_CURRENT, "").strip()
    if env_key:
        return env_key.encode()

    with _key_cache_lock:
        if _cached_current_key is not None:
            return _cached_current_key

    stored = ICP.get_param(_ICP_KEY_CURRENT, "")
    if stored:
        _logger.error(
            "etp_projects: encryption key loaded from database — NOT SAFE "
            "FOR PRODUCTION. Set %s environment variable.",
            _ENV_KEY_CURRENT,
        )
        result = stored.encode()
        with _key_cache_lock:
            _cached_current_key = result
        return result

    key = Fernet.generate_key()
    ICP.set_param(_ICP_KEY_CURRENT, key.decode())
    _logger.warning(
        "etp_projects: generated new Fernet encryption key and stored in "
        "database. For production, set %s environment variable.",
        _ENV_KEY_CURRENT,
    )
    with _key_cache_lock:
        _cached_current_key = key
    return key


def _load_previous_key(ICP) -> bytes | None:
    env_prev = os.environ.get(_ENV_KEY_PREVIOUS, "").strip()
    if env_prev:
        return env_prev.encode()
    stored = ICP.get_param(_ICP_KEY_PREVIOUS, "")
    if stored:
        return stored.encode()
    return None


def _make_fernet(env):
    ICP = env["ir.config_parameter"].sudo()
    current = _load_current_key(ICP)
    previous = _load_previous_key(ICP)
    if previous:
        return MultiFernet([Fernet(current), Fernet(previous)])
    return Fernet(current)


def encrypt_value(env, plaintext: str) -> str:
    if not plaintext:
        return ""
    f = _make_fernet(env)
    token = f.encrypt(plaintext.encode()).decode()
    return f"{_ENCRYPTED_PREFIX}{token}"


def decrypt_value(env, stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(_ENCRYPTED_PREFIX):
        return stored
    token = stored[len(_ENCRYPTED_PREFIX):]
    try:
        f = _make_fernet(env)
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        _logger.error(
            "etp_projects: failed to decrypt stored value. Key mismatch or "
            "data corruption. Returning empty string."
        )
        return ""
