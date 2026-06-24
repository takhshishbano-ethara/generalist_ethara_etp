from __future__ import annotations

import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "fernet:1:"

ENCRYPTED_PARAMS = frozenset({
    "lynceus.anthropic_api_key",
    "lynceus.openrouter_api_key",
})

_key_cache_lock = threading.Lock()
_cached_fernet_key: bytes | None = None


def _get_or_create_key(ICP) -> bytes:
    global _cached_fernet_key
    env_key = os.environ.get("LYNCEUS_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    with _key_cache_lock:
        if _cached_fernet_key is not None:
            return _cached_fernet_key
    key_str = ICP.get_param("lynceus.encryption_key", "")
    if key_str:
        _logger.error(
            "Lynceus: encryption key loaded from database - NOT SAFE FOR "
            "PRODUCTION. Set LYNCEUS_ENCRYPTION_KEY environment variable."
        )
        result = key_str.encode()
        with _key_cache_lock:
            _cached_fernet_key = result
        return result
    key = Fernet.generate_key()
    ICP.set_param("lynceus.encryption_key", key.decode())
    _logger.warning(
        "Lynceus: generated new Fernet encryption key and stored in "
        "database. For production set LYNCEUS_ENCRYPTION_KEY env var."
    )
    with _key_cache_lock:
        _cached_fernet_key = key
    return key


def _get_previous_key(ICP) -> bytes | None:
    env_prev = os.environ.get("LYNCEUS_ENCRYPTION_KEY_PREVIOUS", "").strip()
    if env_prev:
        return env_prev.encode()
    prev_str = ICP.get_param("lynceus.encryption_key_previous", "")
    if prev_str:
        return prev_str.encode()
    return None


def _make_fernet(ICP) -> Fernet | MultiFernet:
    current = _get_or_create_key(ICP)
    previous = _get_previous_key(ICP)
    if previous:
        return MultiFernet([Fernet(current), Fernet(previous)])
    return Fernet(current)


def encrypt(ICP, plaintext: str) -> str:
    if not plaintext:
        return ""
    fernet = _make_fernet(ICP)
    token = fernet.encrypt(plaintext.encode())
    return _ENCRYPTED_PREFIX + token.decode()


def decrypt(ICP, stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(_ENCRYPTED_PREFIX):
        return stored
    token = stored[len(_ENCRYPTED_PREFIX):].encode()
    try:
        return _make_fernet(ICP).decrypt(token).decode()
    except InvalidToken:
        _logger.error(
            "Lynceus: failed to decrypt stored credential - encryption key "
            "may have been rotated without LYNCEUS_ENCRYPTION_KEY_PREVIOUS."
        )
        return ""


def is_set(ICP, param_key: str) -> bool:
    return bool(ICP.get_param(param_key, ""))
