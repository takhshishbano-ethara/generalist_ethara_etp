"""Tests for ``models/credential_manager.py`` + the Settings get/set mask."""

from odoo.tests.common import tagged

from .common import API_KEY_PARAM, IrisCase
from odoo.addons.iris.models import credential_manager

_MASK = "********"


@tagged("post_install", "-at_install", "iris")
class TestCredentialManager(IrisCase):
    def _icp(self):
        return self.env["ir.config_parameter"].sudo()

    # ------------------------------------------------------------------
    # Round trip via the Settings model
    # ------------------------------------------------------------------
    def test_settings_set_get_round_trip(self):
        settings = self.env["res.config.settings"].create({
            "iris_openrouter_api_key": "sk-round-trip-secret",
        })
        settings.set_values()

        # Stored value is Fernet-encrypted, never the plaintext.
        stored = self._icp().get_param(API_KEY_PARAM)
        self.assertTrue(stored.startswith("fernet:1:"))
        self.assertNotIn("sk-round-trip-secret", stored)

        # Transparent decryption on read.
        self.assertEqual(
            credential_manager.get_openrouter_api_key(self.env),
            "sk-round-trip-secret",
        )

        # The form only ever shows the mask once a key exists.
        values = self.env["res.config.settings"].get_values()
        self.assertEqual(values["iris_openrouter_api_key"], _MASK)

    def test_submitting_mask_does_not_overwrite_key(self):
        first = self.env["res.config.settings"].create({
            "iris_openrouter_api_key": "sk-original",
        })
        first.set_values()
        second = self.env["res.config.settings"].create({
            "iris_openrouter_api_key": _MASK,
        })
        second.set_values()
        self.assertEqual(
            credential_manager.get_openrouter_api_key(self.env), "sk-original",
        )

    def test_submitting_new_value_rotates_key(self):
        for secret in ("sk-old", "sk-new"):
            settings = self.env["res.config.settings"].create({
                "iris_openrouter_api_key": secret,
            })
            settings.set_values()
        self.assertEqual(
            credential_manager.get_openrouter_api_key(self.env), "sk-new",
        )

    # ------------------------------------------------------------------
    # Direct encrypt/decrypt helpers
    # ------------------------------------------------------------------
    def test_direct_round_trip(self):
        credential_manager.set_encrypted_param(
            self.env, API_KEY_PARAM, "sk-direct",
        )
        self.assertEqual(
            credential_manager.get_encrypted_param(self.env, API_KEY_PARAM),
            "sk-direct",
        )

    def test_unset_key_returns_empty_without_crash(self):
        self._clear_api_key()
        self.assertEqual(
            credential_manager.get_openrouter_api_key(self.env), "",
        )

    def test_empty_plaintext_stores_empty(self):
        credential_manager.set_encrypted_param(self.env, API_KEY_PARAM, "")
        self.assertEqual(
            credential_manager.get_openrouter_api_key(self.env), "",
        )

    def test_legacy_plaintext_passes_through(self):
        # Values without the fernet:1: prefix are returned as-is (migration
        # path for keys that predate encryption-at-rest).
        self._icp().set_param(API_KEY_PARAM, "sk-legacy-plaintext")
        self.assertEqual(
            credential_manager.get_openrouter_api_key(self.env),
            "sk-legacy-plaintext",
        )

    def test_corrupted_token_returns_empty(self):
        self._icp().set_param(API_KEY_PARAM, "fernet:1:not-a-real-token")
        self.assertEqual(
            credential_manager.get_openrouter_api_key(self.env), "",
        )

    def test_non_encrypted_param_is_stored_plain(self):
        credential_manager.set_encrypted_param(
            self.env, "iris.llm_model", "moonshotai/kimi-k2",
        )
        self.assertEqual(
            self._icp().get_param("iris.llm_model"), "moonshotai/kimi-k2",
        )
