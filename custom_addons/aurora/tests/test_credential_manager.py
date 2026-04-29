# -*- coding: utf-8 -*-
import os
from unittest.mock import patch, MagicMock, call

from cryptography.fernet import Fernet, MultiFernet, InvalidToken

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCredentialManager(TransactionCase):
    """Unit tests for aurora.models.credential_manager."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ICP = cls.env["ir.config_parameter"].sudo()
        # Ensure a key exists for round-trip tests
        cls._test_key = Fernet.generate_key()
        cls._test_key2 = Fernet.generate_key()

    # ═══════════════════════════════════════════════════════════════════════════
    # Constants
    # ═══════════════════════════════════════════════════════════════════════════

    def test_encrypted_prefix_value(self):
        """_ENCRYPTED_PREFIX is 'fernet:1:'."""
        from ..models.credential_manager import _ENCRYPTED_PREFIX
        self.assertEqual(_ENCRYPTED_PREFIX, "fernet:1:")

    def test_encrypted_prefix_is_string(self):
        """_ENCRYPTED_PREFIX is a string."""
        from ..models.credential_manager import _ENCRYPTED_PREFIX
        self.assertIsInstance(_ENCRYPTED_PREFIX, str)

    def test_encrypted_params_is_frozenset(self):
        """ENCRYPTED_PARAMS is a frozenset."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        self.assertIsInstance(ENCRYPTED_PARAMS, frozenset)

    def test_encrypted_params_immutable(self):
        """ENCRYPTED_PARAMS cannot be modified."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        with self.assertRaises(AttributeError):
            ENCRYPTED_PARAMS.add("hack")

    def test_encrypted_params_contains_s3_access_key(self):
        """ENCRYPTED_PARAMS contains aurora.s3_access_key."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        self.assertIn("aurora.s3_access_key", ENCRYPTED_PARAMS)

    def test_encrypted_params_contains_s3_secret_key(self):
        """ENCRYPTED_PARAMS contains aurora.s3_secret_key."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        self.assertIn("aurora.s3_secret_key", ENCRYPTED_PARAMS)

    def test_encrypted_params_length(self):
        """ENCRYPTED_PARAMS has exactly 2 entries."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        self.assertEqual(len(ENCRYPTED_PARAMS), 2)

    def test_encrypted_params_exact_contents(self):
        """ENCRYPTED_PARAMS equals expected set."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        self.assertEqual(ENCRYPTED_PARAMS, frozenset({"aurora.s3_access_key", "aurora.s3_secret_key"}))

    def test_encrypted_params_no_discard(self):
        """ENCRYPTED_PARAMS discard raises AttributeError."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        with self.assertRaises(AttributeError):
            ENCRYPTED_PARAMS.discard("aurora.s3_access_key")

    def test_encrypted_params_no_remove(self):
        """ENCRYPTED_PARAMS remove raises AttributeError."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        with self.assertRaises(AttributeError):
            ENCRYPTED_PARAMS.remove("aurora.s3_access_key")

    def test_encrypted_params_no_pop(self):
        """ENCRYPTED_PARAMS pop raises AttributeError."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        with self.assertRaises(AttributeError):
            ENCRYPTED_PARAMS.pop()

    def test_encrypted_params_no_clear(self):
        """ENCRYPTED_PARAMS clear raises AttributeError."""
        from ..models.credential_manager import ENCRYPTED_PARAMS
        with self.assertRaises(AttributeError):
            ENCRYPTED_PARAMS.clear()

    # ═══════════════════════════════════════════════════════════════════════════
    # _get_or_create_key (ORM path)
    # ═══════════════════════════════════════════════════════════════════════════

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
    def test_get_or_create_key_env_var_set(self):
        """Returns env var when AURORA_ENCRYPTION_KEY is set."""
        from ..models.credential_manager import _get_or_create_key
        key = self._test_key.decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key}):
            result = _get_or_create_key(self.ICP)
        self.assertEqual(result, key.encode())

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
    def test_get_or_create_key_empty_env_uses_db(self):
        """Empty env var falls through to DB lookup."""
        from ..models.credential_manager import _get_or_create_key
        db_key = self._test_key.decode()
        self.ICP.set_param("aurora.encryption_key", db_key)
        result = _get_or_create_key(self.ICP)
        self.assertEqual(result, db_key.encode())

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
    def test_get_or_create_key_generates_when_missing(self):
        """Generates new key when neither env nor DB has one."""
        from ..models.credential_manager import _get_or_create_key
        self.ICP.set_param("aurora.encryption_key", "")
        result = _get_or_create_key(self.ICP)
        self.assertIsInstance(result, bytes)
        self.assertTrue(len(result) > 0)
        # Verify it's a valid Fernet key
        Fernet(result)

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
    def test_get_or_create_key_stores_generated_key(self):
        """Generated key is stored in DB."""
        from ..models.credential_manager import _get_or_create_key
        self.ICP.set_param("aurora.encryption_key", "")
        result = _get_or_create_key(self.ICP)
        stored = self.ICP.get_param("aurora.encryption_key", "")
        self.assertEqual(stored, result.decode())

    def test_get_or_create_key_env_var_takes_priority(self):
        """Env var takes priority over DB key."""
        from ..models.credential_manager import _get_or_create_key
        env_key = Fernet.generate_key().decode()
        db_key = Fernet.generate_key().decode()
        self.ICP.set_param("aurora.encryption_key", db_key)
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": env_key}):
            result = _get_or_create_key(self.ICP)
        self.assertEqual(result, env_key.encode())

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": "   "}, clear=False)
    def test_get_or_create_key_whitespace_env_ignored(self):
        """Whitespace-only env var is treated as empty."""
        from ..models.credential_manager import _get_or_create_key
        db_key = self._test_key.decode()
        self.ICP.set_param("aurora.encryption_key", db_key)
        result = _get_or_create_key(self.ICP)
        self.assertEqual(result, db_key.encode())

    # ═══════════════════════════════════════════════════════════════════════════
    # _get_previous_key (ORM path)
    # ═══════════════════════════════════════════════════════════════════════════

    def test_get_previous_key_env_var_set(self):
        """Returns env var when AURORA_ENCRYPTION_KEY_PREVIOUS is set."""
        from ..models.credential_manager import _get_previous_key
        key = self._test_key2.decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": key}):
            result = _get_previous_key(self.ICP)
        self.assertEqual(result, key.encode())

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
    def test_get_previous_key_db_param_exists(self):
        """Falls through to DB when env var empty."""
        from ..models.credential_manager import _get_previous_key
        db_key = self._test_key2.decode()
        self.ICP.set_param("aurora.encryption_key_previous", db_key)
        result = _get_previous_key(self.ICP)
        self.assertEqual(result, db_key.encode())

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
    def test_get_previous_key_none_when_missing(self):
        """Returns None when neither env nor DB has previous key."""
        from ..models.credential_manager import _get_previous_key
        self.ICP.set_param("aurora.encryption_key_previous", "")
        result = _get_previous_key(self.ICP)
        self.assertIsNone(result)

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": "   "}, clear=False)
    def test_get_previous_key_whitespace_env_ignored(self):
        """Whitespace-only env var treated as empty."""
        from ..models.credential_manager import _get_previous_key
        self.ICP.set_param("aurora.encryption_key_previous", "")
        result = _get_previous_key(self.ICP)
        self.assertIsNone(result)

    # ═══════════════════════════════════════════════════════════════════════════
    # _make_fernet (ORM path)
    # ═══════════════════════════════════════════════════════════════════════════

    def test_make_fernet_single_key(self):
        """Returns Fernet when only current key exists."""
        from ..models.credential_manager import _make_fernet
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            self.ICP.set_param("aurora.encryption_key_previous", "")
            result = _make_fernet(self.ICP)
        self.assertIsInstance(result, Fernet)

    def test_make_fernet_with_previous_key(self):
        """Returns MultiFernet when previous key exists."""
        from ..models.credential_manager import _make_fernet
        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()
        with patch.dict(os.environ, {
            "AURORA_ENCRYPTION_KEY": key1,
            "AURORA_ENCRYPTION_KEY_PREVIOUS": key2,
        }):
            result = _make_fernet(self.ICP)
        self.assertIsInstance(result, MultiFernet)

    # ═══════════════════════════════════════════════════════════════════════════
    # _get_or_create_key_raw (raw cursor path)
    # ═══════════════════════════════════════════════════════════════════════════

    def test_get_or_create_key_raw_env_var_set(self):
        """Raw: returns env var when set."""
        from ..models.credential_manager import _get_or_create_key_raw
        key = self._test_key.decode()
        cr = MagicMock()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key}):
            result = _get_or_create_key_raw(cr)
        self.assertEqual(result, key.encode())
        cr.execute.assert_not_called()

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
    def test_get_or_create_key_raw_db_key_exists(self):
        """Raw: returns DB key when env var empty."""
        from ..models.credential_manager import _get_or_create_key_raw
        db_key = self._test_key.decode()
        cr = MagicMock()
        cr.fetchone.return_value = (db_key,)
        result = _get_or_create_key_raw(cr)
        self.assertEqual(result, db_key.encode())

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
    def test_get_or_create_key_raw_generates_when_missing(self):
        """Raw: generates and stores new key when missing."""
        from ..models.credential_manager import _get_or_create_key_raw
        cr = MagicMock()
        cr.fetchone.return_value = None
        result = _get_or_create_key_raw(cr)
        self.assertIsInstance(result, bytes)
        Fernet(result)  # valid key
        cr.execute.assert_called()
        cr.commit.assert_called()

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": ""}, clear=False)
    def test_get_or_create_key_raw_empty_db_value(self):
        """Raw: empty string in DB treated as missing."""
        from ..models.credential_manager import _get_or_create_key_raw
        cr = MagicMock()
        cr.fetchone.return_value = ("",)
        # Empty string is falsy, should generate new key
        result = _get_or_create_key_raw(cr)
        self.assertIsInstance(result, bytes)

    # ═══════════════════════════════════════════════════════════════════════════
    # _get_previous_key_raw
    # ═══════════════════════════════════════════════════════════════════════════

    def test_get_previous_key_raw_env_var_set(self):
        """Raw: returns env var when set."""
        from ..models.credential_manager import _get_previous_key_raw
        key = self._test_key2.decode()
        cr = MagicMock()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": key}):
            result = _get_previous_key_raw(cr)
        self.assertEqual(result, key.encode())

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
    def test_get_previous_key_raw_db_exists(self):
        """Raw: returns DB key when env var empty."""
        from ..models.credential_manager import _get_previous_key_raw
        cr = MagicMock()
        cr.fetchone.return_value = (self._test_key2.decode(),)
        result = _get_previous_key_raw(cr)
        self.assertEqual(result, self._test_key2)

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
    def test_get_previous_key_raw_none_when_missing(self):
        """Raw: returns None when not found."""
        from ..models.credential_manager import _get_previous_key_raw
        cr = MagicMock()
        cr.fetchone.return_value = None
        result = _get_previous_key_raw(cr)
        self.assertIsNone(result)

    @patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY_PREVIOUS": ""}, clear=False)
    def test_get_previous_key_raw_empty_db_value(self):
        """Raw: empty string in DB returns None."""
        from ..models.credential_manager import _get_previous_key_raw
        cr = MagicMock()
        cr.fetchone.return_value = ("",)
        result = _get_previous_key_raw(cr)
        self.assertIsNone(result)

    # ═══════════════════════════════════════════════════════════════════════════
    # _make_fernet_raw
    # ═══════════════════════════════════════════════════════════════════════════

    def test_make_fernet_raw_single_key(self):
        """Raw: returns Fernet with only current key."""
        from ..models.credential_manager import _make_fernet_raw
        key = Fernet.generate_key().decode()
        cr = MagicMock()
        with patch.dict(os.environ, {
            "AURORA_ENCRYPTION_KEY": key,
            "AURORA_ENCRYPTION_KEY_PREVIOUS": "",
        }):
            cr.fetchone.return_value = None  # no previous key in DB
            result = _make_fernet_raw(cr)
        self.assertIsInstance(result, Fernet)

    def test_make_fernet_raw_with_previous(self):
        """Raw: returns MultiFernet with previous key."""
        from ..models.credential_manager import _make_fernet_raw
        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()
        cr = MagicMock()
        with patch.dict(os.environ, {
            "AURORA_ENCRYPTION_KEY": key1,
            "AURORA_ENCRYPTION_KEY_PREVIOUS": key2,
        }):
            result = _make_fernet_raw(cr)
        self.assertIsInstance(result, MultiFernet)

    # ═══════════════════════════════════════════════════════════════════════════
    # encrypt_value
    # ═══════════════════════════════════════════════════════════════════════════

    def test_encrypt_value_empty_string(self):
        """Empty string returns empty string."""
        from ..models.credential_manager import encrypt_value
        result = encrypt_value(self.ICP, "")
        self.assertEqual(result, "")

    def test_encrypt_value_returns_prefixed(self):
        """Encrypted value starts with fernet:1: prefix."""
        from ..models.credential_manager import encrypt_value, _ENCRYPTED_PREFIX
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = encrypt_value(self.ICP, "my-secret")
        self.assertTrue(result.startswith(_ENCRYPTED_PREFIX))

    def test_encrypt_value_normal_string(self):
        """Encrypting a normal string produces non-empty result."""
        from ..models.credential_manager import encrypt_value
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = encrypt_value(self.ICP, "hello-world")
        self.assertTrue(len(result) > 20)

    def test_encrypt_value_different_inputs_different_outputs(self):
        """Different inputs produce different ciphertexts."""
        from ..models.credential_manager import encrypt_value
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            r1 = encrypt_value(self.ICP, "secret-a")
            r2 = encrypt_value(self.ICP, "secret-b")
        self.assertNotEqual(r1, r2)

    def test_encrypt_value_unicode(self):
        """Unicode strings encrypt successfully."""
        from ..models.credential_manager import encrypt_value
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = encrypt_value(self.ICP, "héllo wörld 日本語")
        self.assertTrue(len(result) > 0)

    def test_encrypt_value_special_chars(self):
        """Special characters encrypt successfully."""
        from ..models.credential_manager import encrypt_value
        key = Fernet.generate_key().decode()
        specials = "!@#$%^&*()_+-=[]{}|;':\",./<>?\n\t\r\x00"
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = encrypt_value(self.ICP, specials)
        self.assertTrue(len(result) > 0)

    def test_encrypt_value_long_string(self):
        """Long string (10K chars) encrypts successfully."""
        from ..models.credential_manager import encrypt_value
        key = Fernet.generate_key().decode()
        long_str = "A" * 10000
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = encrypt_value(self.ICP, long_str)
        self.assertTrue(len(result) > 10000)

    def test_encrypt_value_same_input_different_ciphertext(self):
        """Same input produces different ciphertext each time (Fernet uses timestamps)."""
        from ..models.credential_manager import encrypt_value
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            r1 = encrypt_value(self.ICP, "same-input")
            r2 = encrypt_value(self.ICP, "same-input")
        # Fernet tokens include timestamps, so they differ
        self.assertNotEqual(r1, r2)

    def test_encrypt_value_is_string(self):
        """Encrypted value is a string."""
        from ..models.credential_manager import encrypt_value
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = encrypt_value(self.ICP, "test")
        self.assertIsInstance(result, str)

    # ═══════════════════════════════════════════════════════════════════════════
    # decrypt_value
    # ═══════════════════════════════════════════════════════════════════════════

    def test_decrypt_value_empty_string(self):
        """Empty string returns empty string."""
        from ..models.credential_manager import decrypt_value
        result = decrypt_value(self.ICP, "")
        self.assertEqual(result, "")

    def test_decrypt_value_plaintext_migration(self):
        """Non-prefixed string returned as-is (migration path)."""
        from ..models.credential_manager import decrypt_value
        result = decrypt_value(self.ICP, "plain-text-secret")
        self.assertEqual(result, "plain-text-secret")

    def test_decrypt_value_invalid_token(self):
        """Invalid encrypted token returns empty string."""
        from ..models.credential_manager import decrypt_value, _ENCRYPTED_PREFIX
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = decrypt_value(self.ICP, _ENCRYPTED_PREFIX + "invalid-garbage")
        self.assertEqual(result, "")

    def test_decrypt_value_corrupted_data(self):
        """Corrupted ciphertext returns empty string."""
        from ..models.credential_manager import decrypt_value, _ENCRYPTED_PREFIX
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = decrypt_value(self.ICP, _ENCRYPTED_PREFIX + "YWJjZGVm")
        self.assertEqual(result, "")

    # ═══════════════════════════════════════════════════════════════════════════
    # decrypt_value_raw
    # ═══════════════════════════════════════════════════════════════════════════

    def test_decrypt_value_raw_empty_string(self):
        """Raw: empty string returns empty."""
        from ..models.credential_manager import decrypt_value_raw
        cr = MagicMock()
        result = decrypt_value_raw(cr, "")
        self.assertEqual(result, "")

    def test_decrypt_value_raw_plaintext(self):
        """Raw: non-prefixed string returned as-is."""
        from ..models.credential_manager import decrypt_value_raw
        cr = MagicMock()
        result = decrypt_value_raw(cr, "plain-value")
        self.assertEqual(result, "plain-value")

    def test_decrypt_value_raw_invalid_token(self):
        """Raw: invalid token returns empty string."""
        from ..models.credential_manager import decrypt_value_raw, _ENCRYPTED_PREFIX
        key = Fernet.generate_key().decode()
        cr = MagicMock()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            cr.fetchone.return_value = None
            result = decrypt_value_raw(cr, _ENCRYPTED_PREFIX + "garbage")
        self.assertEqual(result, "")

    # ═══════════════════════════════════════════════════════════════════════════
    # Round-trip: encrypt then decrypt
    # ═══════════════════════════════════════════════════════════════════════════

    def _round_trip(self, plaintext):
        """Helper: encrypt then decrypt, verify match."""
        from ..models.credential_manager import encrypt_value, decrypt_value
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            encrypted = encrypt_value(self.ICP, plaintext)
            decrypted = decrypt_value(self.ICP, encrypted)
        return decrypted

    def test_round_trip_simple(self):
        """Round-trip: simple string."""
        self.assertEqual(self._round_trip("hello"), "hello")

    def test_round_trip_empty(self):
        """Round-trip: empty string stays empty."""
        self.assertEqual(self._round_trip(""), "")

    def test_round_trip_unicode(self):
        """Round-trip: unicode preserved."""
        self.assertEqual(self._round_trip("日本語テスト"), "日本語テスト")

    def test_round_trip_special_chars(self):
        """Round-trip: special characters preserved."""
        val = "!@#$%^&*()_+-=[]{}|;':\",.<>/?\n\t"
        self.assertEqual(self._round_trip(val), val)

    def test_round_trip_long_string(self):
        """Round-trip: long string preserved."""
        val = "X" * 50000
        self.assertEqual(self._round_trip(val), val)

    def test_round_trip_whitespace(self):
        """Round-trip: whitespace preserved."""
        self.assertEqual(self._round_trip("  spaces  "), "  spaces  ")

    def test_round_trip_newlines(self):
        """Round-trip: newlines preserved."""
        val = "line1\nline2\nline3"
        self.assertEqual(self._round_trip(val), val)

    def test_round_trip_null_bytes(self):
        """Round-trip: null bytes preserved."""
        val = "before\x00after"
        self.assertEqual(self._round_trip(val), val)

    def test_round_trip_emoji(self):
        """Round-trip: emoji preserved."""
        self.assertEqual(self._round_trip("🔑🔒"), "🔑🔒")

    def test_round_trip_aws_style_key(self):
        """Round-trip: AWS-style access key."""
        val = "AKIAIOSFODNN7EXAMPLE"
        self.assertEqual(self._round_trip(val), val)

    def test_round_trip_aws_style_secret(self):
        """Round-trip: AWS-style secret key."""
        val = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        self.assertEqual(self._round_trip(val), val)

    def test_round_trip_github_token(self):
        """Round-trip: GitHub PAT."""
        val = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
        self.assertEqual(self._round_trip(val), val)

    def test_round_trip_many_values(self):
        """Round-trip: 50 different values all work."""
        values = [
            f"test-value-{i}" for i in range(50)
        ] + [
            "", "a", "ab", " ", "\n", "\t",
            "日本語", "Ñoño", "über", "café",
        ]
        for val in values:
            self.assertEqual(self._round_trip(val), val, f"Failed for: {val!r}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Key rotation with MultiFernet
    # ═══════════════════════════════════════════════════════════════════════════

    def test_key_rotation_decrypt_with_old_key(self):
        """Data encrypted with old key can be decrypted after rotation."""
        from ..models.credential_manager import encrypt_value, decrypt_value
        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()
        # Encrypt with old key
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": old_key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            encrypted = encrypt_value(self.ICP, "my-secret")
        # Decrypt with new key + old key as previous
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": new_key, "AURORA_ENCRYPTION_KEY_PREVIOUS": old_key}):
            decrypted = decrypt_value(self.ICP, encrypted)
        self.assertEqual(decrypted, "my-secret")

    def test_key_rotation_new_key_works(self):
        """Data encrypted with new key decrypts with MultiFernet too."""
        from ..models.credential_manager import encrypt_value, decrypt_value
        new_key = Fernet.generate_key().decode()
        old_key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": new_key, "AURORA_ENCRYPTION_KEY_PREVIOUS": old_key}):
            encrypted = encrypt_value(self.ICP, "new-secret")
            decrypted = decrypt_value(self.ICP, encrypted)
        self.assertEqual(decrypted, "new-secret")

    def test_key_rotation_wrong_key_fails(self):
        """Data encrypted with unknown key returns empty."""
        from ..models.credential_manager import encrypt_value, decrypt_value
        key_a = Fernet.generate_key().decode()
        key_b = Fernet.generate_key().decode()
        key_c = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key_a, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            encrypted = encrypt_value(self.ICP, "secret")
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key_b, "AURORA_ENCRYPTION_KEY_PREVIOUS": key_c}):
            decrypted = decrypt_value(self.ICP, encrypted)
        self.assertEqual(decrypted, "")

    # ═══════════════════════════════════════════════════════════════════════════
    # set_encrypted_param
    # ═══════════════════════════════════════════════════════════════════════════

    def test_set_encrypted_param_encrypts_s3_access_key(self):
        """s3_access_key is encrypted before storage."""
        from ..models.credential_manager import set_encrypted_param, _ENCRYPTED_PREFIX
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            set_encrypted_param(self.env, "aurora.s3_access_key", "AKIATEST")
        stored = self.ICP.get_param("aurora.s3_access_key", "")
        self.assertTrue(stored.startswith(_ENCRYPTED_PREFIX))

    def test_set_encrypted_param_encrypts_s3_secret_key(self):
        """s3_secret_key is encrypted before storage."""
        from ..models.credential_manager import set_encrypted_param, _ENCRYPTED_PREFIX
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            set_encrypted_param(self.env, "aurora.s3_secret_key", "secret123")
        stored = self.ICP.get_param("aurora.s3_secret_key", "")
        self.assertTrue(stored.startswith(_ENCRYPTED_PREFIX))

    def test_set_encrypted_param_plain_for_non_encrypted(self):
        """Non-encrypted params stored as plaintext."""
        from ..models.credential_manager import set_encrypted_param
        set_encrypted_param(self.env, "aurora.output_dir", "/tmp/test")
        stored = self.ICP.get_param("aurora.output_dir", "")
        self.assertEqual(stored, "/tmp/test")

    def test_set_encrypted_param_empty_value(self):
        """Empty value for encrypted param stores empty encrypted string."""
        from ..models.credential_manager import set_encrypted_param
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            set_encrypted_param(self.env, "aurora.s3_access_key", "")
        stored = self.ICP.get_param("aurora.s3_access_key", "")
        self.assertEqual(stored, "")

    # ═══════════════════════════════════════════════════════════════════════════
    # get_encrypted_param
    # ═══════════════════════════════════════════════════════════════════════════

    def test_get_encrypted_param_decrypts(self):
        """Encrypted param is decrypted on read."""
        from ..models.credential_manager import set_encrypted_param, get_encrypted_param
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            set_encrypted_param(self.env, "aurora.s3_access_key", "AKIATEST")
            result = get_encrypted_param(self.env, "aurora.s3_access_key")
        self.assertEqual(result, "AKIATEST")

    def test_get_encrypted_param_plain_passthrough(self):
        """Non-encrypted param returned as-is."""
        from ..models.credential_manager import get_encrypted_param
        self.ICP.set_param("aurora.output_dir", "/tmp/x")
        result = get_encrypted_param(self.env, "aurora.output_dir")
        self.assertEqual(result, "/tmp/x")

    def test_get_encrypted_param_default(self):
        """Default value used when param missing."""
        from ..models.credential_manager import get_encrypted_param
        result = get_encrypted_param(self.env, "aurora.nonexistent_param", "default-val")
        self.assertEqual(result, "default-val")

    def test_get_encrypted_param_round_trip_all_encrypted(self):
        """Round-trip for all ENCRYPTED_PARAMS."""
        from ..models.credential_manager import set_encrypted_param, get_encrypted_param, ENCRYPTED_PARAMS
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            for param in ENCRYPTED_PARAMS:
                set_encrypted_param(self.env, param, f"value-for-{param}")
            for param in ENCRYPTED_PARAMS:
                result = get_encrypted_param(self.env, param)
                self.assertEqual(result, f"value-for-{param}")

    # ═══════════════════════════════════════════════════════════════════════════
    # get_encrypted_param_raw
    # ═══════════════════════════════════════════════════════════════════════════

    def test_get_encrypted_param_raw_decrypts(self):
        """Raw: encrypted param decrypted correctly."""
        from ..models.credential_manager import get_encrypted_param_raw, _ENCRYPTED_PREFIX
        key = Fernet.generate_key()
        f = Fernet(key)
        encrypted = f.encrypt(b"raw-secret").decode()
        stored = _ENCRYPTED_PREFIX + encrypted
        cr = MagicMock()
        cr.fetchone.return_value = (stored,)
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key.decode(), "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = get_encrypted_param_raw(cr, "aurora.s3_access_key")
        self.assertEqual(result, "raw-secret")

    def test_get_encrypted_param_raw_default(self):
        """Raw: returns default when param not found."""
        from ..models.credential_manager import get_encrypted_param_raw
        cr = MagicMock()
        cr.fetchone.return_value = None
        result = get_encrypted_param_raw(cr, "aurora.output_dir", "my-default")
        self.assertEqual(result, "my-default")

    def test_get_encrypted_param_raw_non_encrypted_passthrough(self):
        """Raw: non-encrypted param returned as-is."""
        from ..models.credential_manager import get_encrypted_param_raw
        cr = MagicMock()
        cr.fetchone.return_value = ("/tmp/output",)
        result = get_encrypted_param_raw(cr, "aurora.output_dir")
        self.assertEqual(result, "/tmp/output")

    def test_get_encrypted_param_raw_encrypted_plaintext_migration(self):
        """Raw: encrypted param stored as plaintext (migration) returned as-is."""
        from ..models.credential_manager import get_encrypted_param_raw
        cr = MagicMock()
        cr.fetchone.return_value = ("old-plain-secret",)
        result = get_encrypted_param_raw(cr, "aurora.s3_access_key")
        self.assertEqual(result, "old-plain-secret")

    # ═══════════════════════════════════════════════════════════════════════════
    # Parametric edge cases
    # ═══════════════════════════════════════════════════════════════════════════

    def test_encrypt_decrypt_various_lengths(self):
        """Encrypt/decrypt works for strings of length 1 to 1000."""
        from ..models.credential_manager import encrypt_value, decrypt_value
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            for length in [1, 2, 5, 10, 50, 100, 500, 1000]:
                val = "x" * length
                enc = encrypt_value(self.ICP, val)
                dec = decrypt_value(self.ICP, enc)
                self.assertEqual(dec, val, f"Failed for length {length}")

    def test_encrypt_decrypt_all_ascii_printable(self):
        """Encrypt/decrypt works for all printable ASCII chars."""
        from ..models.credential_manager import encrypt_value, decrypt_value
        import string
        key = Fernet.generate_key().decode()
        val = string.printable
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            enc = encrypt_value(self.ICP, val)
            dec = decrypt_value(self.ICP, enc)
        self.assertEqual(dec, val)

    def test_encrypt_decrypt_binary_like_string(self):
        """Encrypt/decrypt with binary-like content."""
        from ..models.credential_manager import encrypt_value, decrypt_value
        key = Fernet.generate_key().decode()
        val = "".join(chr(i) for i in range(1, 128))
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            enc = encrypt_value(self.ICP, val)
            dec = decrypt_value(self.ICP, enc)
        self.assertEqual(dec, val)

    def test_decrypt_value_none_stored(self):
        """decrypt_value with None-ish input returns empty."""
        from ..models.credential_manager import decrypt_value
        self.assertEqual(decrypt_value(self.ICP, ""), "")

    def test_encrypt_prefix_not_in_plaintext(self):
        """Encrypted prefix doesn't appear in normal plaintext."""
        from ..models.credential_manager import _ENCRYPTED_PREFIX
        self.assertFalse("hello-world".startswith(_ENCRYPTED_PREFIX))

    def test_encrypt_value_result_is_decodable(self):
        """Encrypted value is valid UTF-8 string."""
        from ..models.credential_manager import encrypt_value
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            result = encrypt_value(self.ICP, "test")
        result.encode("utf-8")  # Should not raise

    def test_multiple_sequential_encryptions(self):
        """Multiple sequential encryptions all produce valid output."""
        from ..models.credential_manager import encrypt_value, decrypt_value
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            for i in range(100):
                val = f"secret-{i}"
                enc = encrypt_value(self.ICP, val)
                dec = decrypt_value(self.ICP, enc)
                self.assertEqual(dec, val)

    # ═══════════════════════════════════════════════════════════════════════════
    # set/get round-trip integration
    # ═══════════════════════════════════════════════════════════════════════════

    def test_set_get_round_trip_s3_access_key(self):
        """set_encrypted_param + get_encrypted_param round-trip for s3_access_key."""
        from ..models.credential_manager import set_encrypted_param, get_encrypted_param
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            set_encrypted_param(self.env, "aurora.s3_access_key", "AKIAIOSFODNN7EXAMPLE")
            result = get_encrypted_param(self.env, "aurora.s3_access_key")
        self.assertEqual(result, "AKIAIOSFODNN7EXAMPLE")

    def test_set_get_round_trip_s3_secret_key(self):
        """set_encrypted_param + get_encrypted_param round-trip for s3_secret_key."""
        from ..models.credential_manager import set_encrypted_param, get_encrypted_param
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            set_encrypted_param(self.env, "aurora.s3_secret_key", "wJalrXUtnFEMI/K7MDENG")
            result = get_encrypted_param(self.env, "aurora.s3_secret_key")
        self.assertEqual(result, "wJalrXUtnFEMI/K7MDENG")

    def test_set_get_overwrite(self):
        """Overwriting encrypted param works."""
        from ..models.credential_manager import set_encrypted_param, get_encrypted_param
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            set_encrypted_param(self.env, "aurora.s3_access_key", "OLD")
            set_encrypted_param(self.env, "aurora.s3_access_key", "NEW")
            result = get_encrypted_param(self.env, "aurora.s3_access_key")
        self.assertEqual(result, "NEW")

    def test_set_get_multiple_params(self):
        """Multiple encrypted params coexist."""
        from ..models.credential_manager import set_encrypted_param, get_encrypted_param
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"AURORA_ENCRYPTION_KEY": key, "AURORA_ENCRYPTION_KEY_PREVIOUS": ""}):
            set_encrypted_param(self.env, "aurora.s3_access_key", "KEY-A")
            set_encrypted_param(self.env, "aurora.s3_secret_key", "KEY-B")
            self.assertEqual(get_encrypted_param(self.env, "aurora.s3_access_key"), "KEY-A")
            self.assertEqual(get_encrypted_param(self.env, "aurora.s3_secret_key"), "KEY-B")
