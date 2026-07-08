from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestAwsCredentials(TransactionCase):

    def test_roundtrip_encryption(self):
        creds = self.env["etp.aws.credentials"].get_singleton()
        secret = "test_secret_key_ABC123xyz"
        creds.write({"secret_key": secret})
        self.assertNotEqual(creds.secret_key_encrypted, secret)
        self.assertTrue(creds.secret_key_encrypted.startswith("fernet:1:"))
        self.assertEqual(creds.secret_key, secret)

    def test_singleton_reuse(self):
        rec_a = self.env["etp.aws.credentials"].get_singleton()
        rec_b = self.env["etp.aws.credentials"].get_singleton()
        self.assertEqual(rec_a.id, rec_b.id)

    def test_second_create_rejected(self):
        self.env["etp.aws.credentials"].get_singleton()
        with self.assertRaises(ValidationError):
            self.env["etp.aws.credentials"].create({"access_key_id": "AKIADUPLICATE"})

    def test_unlink_rejected(self):
        creds = self.env["etp.aws.credentials"].get_singleton()
        with self.assertRaises(ValidationError):
            creds.unlink()

    def test_get_credentials_disabled_returns_empty(self):
        creds = self.env["etp.aws.credentials"].get_singleton()
        creds.write({
            "is_enabled": False,
            "access_key_id": "AKIATEST",
            "secret_key": "sec",
        })
        self.assertEqual(self.env["etp.aws.credentials"].get_credentials(), {})

    def test_get_credentials_enabled_returns_decrypted(self):
        creds = self.env["etp.aws.credentials"].get_singleton()
        creds.write({
            "is_enabled": True,
            "access_key_id": "AKIATEST",
            "secret_key": "supersecret",
            "region_name": "eu-west-1",
        })
        result = self.env["etp.aws.credentials"].get_credentials()
        self.assertEqual(result["aws_access_key_id"], "AKIATEST")
        self.assertEqual(result["aws_secret_access_key"], "supersecret")
        self.assertEqual(result["region_name"], "eu-west-1")

    def test_empty_secret_encrypts_to_empty(self):
        creds = self.env["etp.aws.credentials"].get_singleton()
        creds.write({"secret_key": ""})
        self.assertEqual(creds.secret_key_encrypted or "", "")
        self.assertEqual(creds.secret_key or "", "")

    def test_settings_get_values_never_exposes_decrypted_secret(self):
        creds = self.env["etp.aws.credentials"].get_singleton()
        creds.write({"secret_key": "SUPER_SENSITIVE_ABC123"})

        settings = self.env["res.config.settings"].create({})
        values = settings.get_values()

        self.assertEqual(
            values.get("etp_aws_secret_access_key"), "",
            "get_values must never populate the secret field",
        )

    def test_settings_blank_secret_preserves_existing(self):
        creds = self.env["etp.aws.credentials"].get_singleton()
        creds.write({"secret_key": "original_secret_ABC123"})
        original_encrypted = creds.secret_key_encrypted

        settings = self.env["res.config.settings"].create({
            "etp_aws_is_enabled": True,
            "etp_aws_access_key_id": "AKIAKEEP",
            "etp_aws_secret_access_key": "",
            "etp_aws_region_name": "us-east-1",
        })
        settings.set_values()

        creds.invalidate_recordset()
        self.assertEqual(
            creds.secret_key_encrypted, original_encrypted,
            "blank secret input must NOT overwrite the stored encrypted value",
        )
        self.assertEqual(creds.secret_key, "original_secret_ABC123")

    def test_settings_new_secret_updates_existing(self):
        creds = self.env["etp.aws.credentials"].get_singleton()
        creds.write({"secret_key": "old_secret"})

        settings = self.env["res.config.settings"].create({
            "etp_aws_is_enabled": True,
            "etp_aws_access_key_id": "AKIAKEEP",
            "etp_aws_secret_access_key": "new_secret_XYZ",
            "etp_aws_region_name": "us-east-1",
        })
        settings.set_values()

        creds.invalidate_recordset()
        self.assertEqual(creds.secret_key, "new_secret_XYZ")
