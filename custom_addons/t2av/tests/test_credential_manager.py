import os
from unittest import mock

from cryptography.fernet import Fernet

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.t2av.models import credential_manager
from odoo.addons.t2av.models.credential_manager import (
    decrypt_value,
    encrypt_value,
    get_encrypted_param,
    get_openrouter_api_key,
    set_encrypted_param,
)


@tagged("post_install", "-at_install", "t2av")
class TestCredentialManager(TransactionCase):
    def setUp(self):
        super().setUp()
        # Always start with a clean module-level key cache so per-test env
        # overrides take effect.
        credential_manager._cached_fernet_key = None
        credential_manager._cached_fernet_key_raw = None
        self.addCleanup(self._reset_cache)

    def _reset_cache(self):
        credential_manager._cached_fernet_key = None
        credential_manager._cached_fernet_key_raw = None

    def _icp(self):
        return self.env["ir.config_parameter"].sudo()

    def test_encrypt_decrypt_roundtrip(self):
        key = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {"T2AV_ENCRYPTION_KEY": key}, clear=False):
            credential_manager._cached_fernet_key = None
            ICP = self._icp()
            encrypted = encrypt_value(ICP, "secret-abc")
            self.assertTrue(encrypted.startswith("fernet:1:"))
            self.assertNotIn("secret-abc", encrypted)
            self.assertEqual(decrypt_value(ICP, encrypted), "secret-abc")

    def test_decrypt_unencrypted_returns_raw(self):
        key = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {"T2AV_ENCRYPTION_KEY": key}, clear=False):
            credential_manager._cached_fernet_key = None
            ICP = self._icp()
            # No fernet:1: prefix → returned unchanged for backward compat.
            self.assertEqual(decrypt_value(ICP, "plain-value"), "plain-value")
            self.assertEqual(decrypt_value(ICP, ""), "")

    def test_get_encrypted_param_returns_decrypted(self):
        key = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {"T2AV_ENCRYPTION_KEY": key}, clear=False):
            credential_manager._cached_fernet_key = None
            set_encrypted_param(self.env, "t2av.openrouter_api_key", "my-key")
            # Raw value in DB must be encrypted (fernet:1: prefix).
            raw = self._icp().get_param("t2av.openrouter_api_key")
            self.assertTrue(raw.startswith("fernet:1:"))
            self.assertNotIn("my-key", raw)
            # Read-through getter decrypts.
            self.assertEqual(
                get_encrypted_param(self.env, "t2av.openrouter_api_key"),
                "my-key",
            )

    def test_get_openrouter_api_key_helper(self):
        key = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {"T2AV_ENCRYPTION_KEY": key}, clear=False):
            credential_manager._cached_fernet_key = None
            set_encrypted_param(self.env, "t2av.openrouter_api_key", "sk-or-xyz")
            self.assertEqual(get_openrouter_api_key(self.env), "sk-or-xyz")

    def test_key_rotation_via_previous(self):
        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()

        # Step 1: encrypt with key1 as the current key.
        with mock.patch.dict(os.environ, {"T2AV_ENCRYPTION_KEY": key1}, clear=False):
            credential_manager._cached_fernet_key = None
            ICP = self._icp()
            ciphertext = encrypt_value(ICP, "rotating-secret")
            self.assertTrue(ciphertext.startswith("fernet:1:"))

        # Step 2: rotate — key2 becomes current, key1 becomes previous.
        rotated_env = {
            "T2AV_ENCRYPTION_KEY": key2,
            "T2AV_ENCRYPTION_KEY_PREVIOUS": key1,
        }
        with mock.patch.dict(os.environ, rotated_env, clear=False):
            credential_manager._cached_fernet_key = None
            ICP = self._icp()
            # MultiFernet should still decrypt the key1-encrypted ciphertext.
            self.assertEqual(decrypt_value(ICP, ciphertext), "rotating-secret")
