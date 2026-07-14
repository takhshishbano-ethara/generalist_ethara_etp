"""Fernet encryption-at-rest for Ethara HRMS credentials.

Values listed in ``ENCRYPTED_PARAMS`` are stored in ``ir.config_parameter``
as ``fernet:1:<token>`` and transparently decrypted on read.

Key resolution order:
1. ``ETHARA_HRMS_ENCRYPTION_KEY`` environment variable (production),
2. ``ethara_hrms.encryption_key`` ICP value (dev only — logged as unsafe),
3. auto-generated Fernet key persisted to the ICP (dev bootstrap).

Key rotation: set ``ETHARA_HRMS_ENCRYPTION_KEY`` to the new key and
``ETHARA_HRMS_ENCRYPTION_KEY_PREVIOUS`` (or ICP
``ethara_hrms.encryption_key_previous``) to the old one; ``MultiFernet``
decrypts values written under either key.
"""

from __future__ import annotations

import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "fernet:1:"

ENCRYPTED_PARAMS = frozenset({
    "ethara_hrms.llm_api_key",
})

_key_cache_lock = threading.Lock()
_cached_fernet_keys: dict[str, bytes] = {}


def _get_or_create_key(ICP) -> bytes:
    env_key = os.environ.get("ETHARA_HRMS_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()
    dbname = ICP.env.cr.dbname
    with _key_cache_lock:
        cached = _cached_fernet_keys.get(dbname)
    if cached is not None:
        return cached
    key_str = ICP.get_param("ethara_hrms.encryption_key", "")
    if key_str:
        _logger.error(
            "Ethara HRMS: encryption key loaded from database — NOT SAFE "
            "FOR PRODUCTION. Set ETHARA_HRMS_ENCRYPTION_KEY env var."
        )
        result = key_str.encode()
        with _key_cache_lock:
            _cached_fernet_keys[dbname] = result
        return result
    key = Fernet.generate_key()
    ICP.set_param("ethara_hrms.encryption_key", key.decode())
    _logger.warning(
        "Ethara HRMS: generated new Fernet encryption key and stored in DB. "
        "For production, set ETHARA_HRMS_ENCRYPTION_KEY env var."
    )
    with _key_cache_lock:
        _cached_fernet_keys[dbname] = key
    return key


def _get_previous_key(ICP) -> bytes | None:
    env_prev = os.environ.get("ETHARA_HRMS_ENCRYPTION_KEY_PREVIOUS", "").strip()
    if env_prev:
        return env_prev.encode()
    prev_str = ICP.get_param("ethara_hrms.encryption_key_previous", "")
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
    return f"{_ENCRYPTED_PREFIX}{f.encrypt(plaintext.encode()).decode()}"


def decrypt_value(ICP, stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(_ENCRYPTED_PREFIX):
        return stored
    token = stored[len(_ENCRYPTED_PREFIX):]
    try:
        return _make_fernet(ICP).decrypt(token.encode()).decode()
    except InvalidToken:
        _logger.error(
            "Ethara HRMS: failed to decrypt config value. Key may have "
            "changed or data is corrupted."
        )
        return ""


def get_encrypted_param(env, key: str, default: str = "") -> str:
    ICP = env["ir.config_parameter"].sudo()
    stored = ICP.get_param(key, default)
    if key in ENCRYPTED_PARAMS:
        decrypted = decrypt_value(ICP, stored)
        if not decrypted and stored:
            _logger.warning(
                "Ethara HRMS: encrypted param '%s' is set but decryption "
                "returned empty. Check ETHARA_HRMS_ENCRYPTION_KEY matches "
                "the key used to encrypt.", key,
            )
        return decrypted
    return stored


def set_encrypted_param(env, key: str, plaintext: str) -> None:
    ICP = env["ir.config_parameter"].sudo()
    if key in ENCRYPTED_PARAMS:
        stored = encrypt_value(ICP, plaintext) if plaintext else ""
    else:
        stored = plaintext or ""
    ICP.set_param(key, stored)


def get_llm_api_key(env) -> str:
    return get_encrypted_param(env, "ethara_hrms.llm_api_key")
