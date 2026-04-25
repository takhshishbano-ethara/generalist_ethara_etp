import os
import threading
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet, MultiFernet

import aurora.models.credential_manager as cm
from aurora.models.credential_manager import (
    ENCRYPTED_PARAMS,
    _ENCRYPTED_PREFIX,
    _get_or_create_key,
    _get_or_create_key_raw,
    _get_previous_key,
    _get_previous_key_raw,
    _make_fernet,
    _make_fernet_raw,
    decrypt_value,
    decrypt_value_raw,
    encrypt_value,
    get_encrypted_param,
    get_encrypted_param_raw,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    cm._cached_fernet_key = None
    cm._cached_fernet_key_raw = None
    yield
    cm._cached_fernet_key = None
    cm._cached_fernet_key_raw = None


def _make_icp(params=None):
    store = dict(params) if params else {}
    icp = MagicMock()
    icp.get_param = MagicMock(side_effect=lambda k, default="": store.get(k, default))
    icp.set_param = MagicMock(side_effect=lambda k, v: store.__setitem__(k, v))
    return icp


def _make_cr(rows=None):
    cr = MagicMock()
    results = list(rows) if rows else []
    cr.fetchone = MagicMock(side_effect=lambda: results.pop(0) if results else None)
    cr.execute = MagicMock()
    cr.commit = MagicMock()
    return cr


@pytest.mark.parametrize(
    "plaintext",
    [
        "hello",
        "password123!@#",
        "",
        "a",
        "x" * 5000,
        "日本語テスト",
        "emoji 🎉🔥",
        "line1\nline2\nline3",
        " leading-trailing ",
        "special=chars&more+plus/slash",
        "AKIAIOSFODNN7EXAMPLE",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "\t\ttabs",
        "null\x00byte",
        'quotes"and\'mixed',
        "back\\slash",
    ],
    ids=[
        "simple", "special-chars", "empty", "single-char", "long",
        "unicode-jp", "emoji", "multiline", "whitespace-padded",
        "url-like-chars", "aws-access-key", "aws-secret-key",
        "tabs", "null-byte", "quotes", "backslash",
    ],
)
def test_encrypt_decrypt_roundtrip(plaintext, fernet_key):
    icp = _make_icp({"aurora.encryption_key": fernet_key.decode()})
    encrypted = encrypt_value(icp, plaintext)
    if not plaintext:
        assert encrypted == ""
        return
    assert encrypted.startswith(_ENCRYPTED_PREFIX)
    cm._cached_fernet_key = None
    decrypted = decrypt_value(icp, encrypted)
    assert decrypted == plaintext


@pytest.mark.parametrize(
    "plaintext",
    [
        "hello",
        "password123!@#",
        "",
        "a",
        "x" * 5000,
        "日本語テスト",
        "emoji 🎉🔥",
        "line1\nline2",
        " spaces ",
        "AKIAIOSFODNN7EXAMPLE",
    ],
    ids=[
        "simple", "special", "empty", "single", "long",
        "unicode", "emoji", "multiline", "spaces", "aws-key",
    ],
)
def test_encrypt_decrypt_raw_roundtrip(plaintext, fernet_key):
    cr = _make_cr([(fernet_key.decode(),)])
    encrypted = encrypt_value(_make_icp({"aurora.encryption_key": fernet_key.decode()}), plaintext)
    if not plaintext:
        assert encrypted == ""
        return
    cm._cached_fernet_key_raw = None
    cr2 = _make_cr([(fernet_key.decode(),)])
    decrypted = decrypt_value_raw(cr2, encrypted)
    assert decrypted == plaintext


def test_decrypt_value_returns_empty_for_empty():
    icp = _make_icp()
    assert decrypt_value(icp, "") == ""


def test_decrypt_value_returns_plaintext_without_prefix():
    icp = _make_icp()
    assert decrypt_value(icp, "not-encrypted-value") == "not-encrypted-value"


def test_decrypt_value_invalid_token_returns_empty(fernet_key):
    icp = _make_icp({"aurora.encryption_key": fernet_key.decode()})
    bad_stored = f"{_ENCRYPTED_PREFIX}INVALIDTOKENDATA"
    assert decrypt_value(icp, bad_stored) == ""


def test_decrypt_value_raw_returns_empty_for_empty():
    cr = _make_cr()
    assert decrypt_value_raw(cr, "") == ""


def test_decrypt_value_raw_returns_plaintext_without_prefix():
    cr = _make_cr()
    assert decrypt_value_raw(cr, "plain-text") == "plain-text"


def test_decrypt_value_raw_invalid_token_returns_empty(fernet_key):
    cr = _make_cr([(fernet_key.decode(),)])
    bad_stored = f"{_ENCRYPTED_PREFIX}BADDATA"
    assert decrypt_value_raw(cr, bad_stored) == ""


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
def test_get_or_create_key_env_var():
    key = Fernet.generate_key().decode()
    with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key}):
        result = _get_or_create_key(MagicMock())
        assert result == key.encode()


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
def test_get_or_create_key_cached():
    cm._cached_fernet_key = b"cached-key"
    icp = _make_icp()
    result = _get_or_create_key(icp)
    assert result == b"cached-key"
    icp.get_param.assert_not_called()


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
def test_get_or_create_key_from_db():
    key = Fernet.generate_key().decode()
    icp = _make_icp({"aurora.encryption_key": key})
    result = _get_or_create_key(icp)
    assert result == key.encode()
    assert cm._cached_fernet_key == key.encode()


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
def test_get_or_create_key_generates_new():
    icp = _make_icp()
    result = _get_or_create_key(icp)
    assert len(result) > 0
    Fernet(result)
    icp.set_param.assert_called_once()
    assert cm._cached_fernet_key == result


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
def test_get_or_create_key_raw_env_var():
    key = Fernet.generate_key().decode()
    with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key}):
        result = _get_or_create_key_raw(MagicMock())
        assert result == key.encode()


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
def test_get_or_create_key_raw_cached():
    cm._cached_fernet_key_raw = b"raw-cached"
    cr = MagicMock()
    result = _get_or_create_key_raw(cr)
    assert result == b"raw-cached"
    cr.execute.assert_not_called()


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
def test_get_or_create_key_raw_from_db():
    key = Fernet.generate_key().decode()
    cr = _make_cr([(key,)])
    result = _get_or_create_key_raw(cr)
    assert result == key.encode()


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
def test_get_or_create_key_raw_generates_new():
    cr = _make_cr([None])
    result = _get_or_create_key_raw(cr)
    assert len(result) > 0
    Fernet(result)
    cr.commit.assert_called_once()


@pytest.mark.parametrize(
    "env_val, expected_present",
    [
        ("prevkey", True),
        ("", False),
        ("  ", False),
    ],
    ids=["present", "empty", "whitespace"],
)
def test_get_previous_key_env(env_val, expected_present):
    with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": env_val}):
        icp = _make_icp()
        result = _get_previous_key(icp)
        if expected_present:
            assert result == env_val.strip().encode()
        else:
            assert result is None


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
def test_get_previous_key_from_db():
    icp = _make_icp({"aurora.encryption_key_previous": "dbprev"})
    assert _get_previous_key(icp) == b"dbprev"


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
def test_get_previous_key_not_found():
    icp = _make_icp()
    assert _get_previous_key(icp) is None


def test_get_previous_key_raw_env_present():
    with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": "rawprev"}):
        cr = MagicMock()
        result = _get_previous_key_raw(cr)
        assert result == b"rawprev"


@pytest.mark.parametrize(
    "env_val",
    ["", "  "],
    ids=["empty", "whitespace"],
)
def test_get_previous_key_raw_env_absent(env_val):
    with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": env_val}):
        cr = _make_cr([None])
        result = _get_previous_key_raw(cr)
        assert result is None


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
def test_get_previous_key_raw_from_db():
    cr = _make_cr([("dbrawprev",)])
    assert _get_previous_key_raw(cr) == b"dbrawprev"


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
def test_get_previous_key_raw_not_found():
    cr = _make_cr([None])
    assert _get_previous_key_raw(cr) is None


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": "", "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
def test_make_fernet_without_previous():
    key = Fernet.generate_key().decode()
    icp = _make_icp({"aurora.encryption_key": key})
    f = _make_fernet(icp)
    assert isinstance(f, Fernet)


def test_make_fernet_with_previous():
    key1 = Fernet.generate_key().decode()
    key2 = Fernet.generate_key().decode()
    with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key1, "AURORA_ENCRYPTION_KEY_PREVIOUS": key2}):
        icp = MagicMock()
        f = _make_fernet(icp)
        assert isinstance(f, MultiFernet)


@patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": "", "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
def test_make_fernet_raw_without_previous():
    key = Fernet.generate_key().decode()
    cr = _make_cr([(key,), None])
    f = _make_fernet_raw(cr)
    assert isinstance(f, Fernet)


def test_make_fernet_raw_with_previous():
    key1 = Fernet.generate_key().decode()
    key2 = Fernet.generate_key().decode()
    with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key1, "AURORA_ENCRYPTION_KEY_PREVIOUS": key2}):
        cr = MagicMock()
        f = _make_fernet_raw(cr)
        assert isinstance(f, MultiFernet)


def test_get_encrypted_param_encrypted_key(fernet_key):
    f = Fernet(fernet_key)
    token = f.encrypt(b"my-secret").decode()
    stored = f"{_ENCRYPTED_PREFIX}{token}"
    icp = MagicMock()
    icp.get_param = MagicMock(side_effect=lambda k, default="": stored if k == "aurora.s3_access_key" else default)
    icp.set_param = MagicMock()
    env = MagicMock()
    env.__getitem__ = MagicMock(return_value=MagicMock(sudo=MagicMock(return_value=icp)))
    with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": fernet_key.decode(), "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
        result = get_encrypted_param(env, "aurora.s3_access_key")
    assert result == "my-secret"


def test_get_encrypted_param_non_encrypted_key():
    icp = _make_icp({"aurora.some_setting": "plain-value"})
    env = MagicMock()
    env.__getitem__ = MagicMock(return_value=MagicMock(sudo=MagicMock(return_value=icp)))
    result = get_encrypted_param(env, "aurora.some_setting")
    assert result == "plain-value"


def test_get_encrypted_param_missing_key():
    icp = _make_icp()
    env = MagicMock()
    env.__getitem__ = MagicMock(return_value=MagicMock(sudo=MagicMock(return_value=icp)))
    result = get_encrypted_param(env, "aurora.nonexistent", default="fallback")
    assert result == "fallback"


def test_get_encrypted_param_raw_encrypted_key(fernet_key):
    f = Fernet(fernet_key)
    token = f.encrypt(b"raw-secret").decode()
    stored = f"{_ENCRYPTED_PREFIX}{token}"
    cr = _make_cr([(stored,), (fernet_key.decode(),)])
    result = get_encrypted_param_raw(cr, "aurora.s3_secret_key")
    assert result == "raw-secret"


def test_get_encrypted_param_raw_non_encrypted_key():
    cr = _make_cr([("some-value",)])
    result = get_encrypted_param_raw(cr, "aurora.other_key")
    assert result == "some-value"


def test_get_encrypted_param_raw_missing_key():
    cr = _make_cr([None])
    result = get_encrypted_param_raw(cr, "aurora.missing", default="def")
    assert result == "def"


def test_encrypted_params_contains_expected_keys():
    assert "aurora.s3_access_key" in ENCRYPTED_PARAMS
    assert "aurora.s3_secret_key" in ENCRYPTED_PARAMS
    assert len(ENCRYPTED_PARAMS) == 2


def test_encrypted_prefix_format():
    assert _ENCRYPTED_PREFIX == "fernet:1:"


def test_encrypt_value_empty_returns_empty(fernet_key):
    icp = _make_icp({"aurora.encryption_key": fernet_key.decode()})
    assert encrypt_value(icp, "") == ""


def test_encrypt_value_produces_unique_ciphertexts(fernet_key):
    icp = _make_icp({"aurora.encryption_key": fernet_key.decode()})
    e1 = encrypt_value(icp, "same")
    cm._cached_fernet_key = None
    e2 = encrypt_value(icp, "same")
    assert e1 != e2


def test_multifernet_can_decrypt_with_old_key():
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_f = Fernet(old_key)
    token = old_f.encrypt(b"legacy-secret").decode()
    stored = f"{_ENCRYPTED_PREFIX}{token}"
    with patch.dict(os.environ, {
        "AURORA_ENCRYPTION_KEY": new_key.decode(),
        "AURORA_ENCRYPTION_KEY_PREVIOUS": old_key.decode(),
    }):
        icp = MagicMock()
        result = decrypt_value(icp, stored)
        assert result == "legacy-secret"
