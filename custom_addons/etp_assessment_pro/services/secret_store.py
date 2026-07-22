# -*- coding: utf-8 -*-
"""M-8: encryption-at-rest for the two high-value secrets the module stores in
``ir.config_parameter`` - the S3 secret access key and the Vertex service-account
JSON (which contains a private key).

Design
------
* A single choke point: ``encrypt`` on write, ``decrypt`` on read. The settings
  page and the two readers (``services/s3_service`` creds, ``services/vertex``
  bearer minting) route their secret access through here.
* Key material is derived from Odoo's own ``database.secret`` (the per-database
  master secret already used to sign sessions), via PBKDF2-HMAC-SHA256 into a
  Fernet key. No new key to manage; a DB dump alone (without the master secret)
  cannot decrypt the values.
* Ciphertext is tagged with a version marker ``enc:v1:``. ``decrypt`` returns any
  value WITHOUT that marker unchanged, so a legacy plaintext secret keeps working
  and is transparently re-encrypted the next time settings are saved (no forced
  migration, no downtime).
* Fail-safe: if ``cryptography`` is somehow unavailable or the key cannot be
  derived, the value is stored/returned as plaintext with a loud log rather than
  breaking S3/Vertex - availability is not sacrificed for the hardening.
"""
import base64
import hashlib
import logging

_logger = logging.getLogger(__name__)

_ENC_PREFIX = "enc:v1:"
_KDF_SALT = b"etp_assessment_pro.secret_store.v1"


def _fernet(env):
    """Build the Fernet cipher from Odoo's database.secret. Returns None if the
    crypto stack or the master secret is unavailable (callers fall back to
    plaintext with a warning)."""
    try:
        from cryptography.fernet import Fernet
    except Exception:  # noqa: BLE001
        return None
    master = (env["ir.config_parameter"].sudo().get_param("database.secret")
              or "").strip()
    if not master:
        return None
    key = hashlib.pbkdf2_hmac("sha256", master.encode("utf-8"), _KDF_SALT,
                              100_000, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


def encrypt(env, plaintext):
    """Encrypt a secret for storage. Empty/blank passes through unchanged (so an
    empty setting stays empty, not an encrypted blob)."""
    if not plaintext:
        return plaintext or ""
    if is_encrypted(plaintext):
        return plaintext
    f = _fernet(env)
    if f is None:
        _logger.warning(
            "etp_assessment secret_store: no cipher available; storing secret "
            "as PLAINTEXT. Set a database.secret and re-save settings.")
        return plaintext
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def decrypt(env, stored):
    """Decrypt a stored secret. A value without the enc marker is legacy plaintext
    and returned as-is. A marked value that fails to decrypt returns '' with a
    loud log (never leaks ciphertext to a caller expecting a real secret)."""
    if not stored or not is_encrypted(stored):
        return stored or ""
    f = _fernet(env)
    if f is None:
        _logger.error(
            "etp_assessment secret_store: encrypted secret present but no cipher "
            "available to decrypt it (missing database.secret or cryptography).")
        return ""
    try:
        token = stored[len(_ENC_PREFIX):].encode("ascii")
        return f.decrypt(token).decode("utf-8")
    except Exception:  # noqa: BLE001
        _logger.error(
            "etp_assessment secret_store: failed to decrypt a stored secret "
            "(wrong database.secret, or corrupted value).")
        return ""


# The ir.config_parameter keys whose values are secret and must be encrypted.
SECRET_PARAM_KEYS = (
    "etp_assessment_pro.s3_secret_key",
    "etp_assessment_pro.vertex_service_account_json",
)


def get_secret(env, key, default=""):
    """Read + decrypt a secret config parameter."""
    raw = env["ir.config_parameter"].sudo().get_param(key, default)
    return decrypt(env, raw)


def set_secret(env, key, plaintext):
    """Encrypt + write a secret config parameter."""
    env["ir.config_parameter"].sudo().set_param(key, encrypt(env, plaintext))
