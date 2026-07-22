# -*- coding: utf-8 -*-
"""Regression tests for the settings-page uploaded-file persistence.

Root cause these lock down: the Service Account JSON (and the question /
scoring .md) uploads used to persist inside an @api.onchange handler. Odoo
runs onchange in a side-effect-free context and rolls its cursor back, so the
ir.config_parameter writes were silently discarded -- the admin uploaded the
file, pressed Save, and nothing stuck ("it does not take it"). Persistence now
lives in set_values(), which runs in the committed Save transaction.
"""
import base64

from odoo.tests.common import TransactionCase, tagged


SA_JSON = (
    '{"type":"service_account","project_id":"agon-development-499205",'
    '"private_key_id":"abc","private_key":"-----BEGIN PRIVATE KEY-----'
    '\\nFAKE\\n-----END PRIVATE KEY-----\\n",'
    '"client_email":"vertex-express@agon-development-499205.iam.gserviceaccount.com",'
    '"token_uri":"https://oauth2.googleapis.com/token"}'
)


@tagged("-at_install", "post_install")
class TestSettingsUploadPersistence(TransactionCase):
    def _icp(self):
        return self.env["ir.config_parameter"].sudo()

    def _b64(self, text):
        return base64.b64encode(text.encode("utf-8"))

    def test_sa_json_upload_persists_on_save(self):
        """Save() must write the uploaded SA JSON to config_parameter."""
        icp = self._icp()
        icp.set_param("etp_assessment_pro.vertex_service_account_json", "")
        icp.set_param("etp_assessment_pro.vertex_service_account_filename", "")

        settings = self.env["res.config.settings"].create({
            "vertex_sa_upload": self._b64(SA_JSON),
            "vertex_sa_upload_filename": "agon.json",
        })
        settings.execute()  # what the Save button calls

        # M-8: the SA JSON is now stored ENCRYPTED at rest. The raw param must be
        # ciphertext (not the plaintext), and decrypting it must round-trip back
        # to the original - proving both persistence and encryption-at-rest.
        from odoo.addons.etp_assessment_pro.services import secret_store
        stored = icp.get_param(
            "etp_assessment_pro.vertex_service_account_json")
        self.assertTrue(
            secret_store.is_encrypted(stored),
            "SA JSON must be stored encrypted at rest, not plaintext")
        self.assertNotIn(
            "private_key", stored,
            "the raw private key must not appear in the stored ciphertext")
        self.assertEqual(
            secret_store.decrypt(self.env, stored), SA_JSON,
            "decrypting the stored SA JSON must round-trip to the original")
        self.assertEqual(
            icp.get_param("etp_assessment_pro.vertex_service_account_filename"),
            "agon.json")

    def test_sa_upload_clears_stale_minted_token(self):
        """A new SA upload must invalidate the cached minted bearer."""
        icp = self._icp()
        icp.set_param("etp_assessment_pro.vertex_minted_token", "STALE")
        icp.set_param(
            "etp_assessment_pro.vertex_minted_token_expires", "9999999999")

        settings = self.env["res.config.settings"].create({
            "vertex_sa_upload": self._b64(SA_JSON),
            "vertex_sa_upload_filename": "agon.json",
        })
        settings.execute()

        self.assertFalse(
            icp.get_param("etp_assessment_pro.vertex_minted_token"),
            "uploading a new SA must clear the cached bearer so it re-mints")

    def test_onchange_does_not_persist_without_save(self):
        """Onchange alone (no Save) must NOT write to config_parameter.

        This is the exact failure mode of the old design; keeping it asserted
        documents WHY persistence had to move to set_values().
        """
        icp = self._icp()
        icp.set_param(
            "etp_assessment_pro.vertex_service_account_filename", "SENTINEL")

        settings = self.env["res.config.settings"].new({
            "vertex_sa_upload": self._b64(SA_JSON),
            "vertex_sa_upload_filename": "agon.json",
        })
        settings._onchange_vertex_sa_upload()
        # In-memory field reflects the upload (instant UI feedback)...
        self.assertEqual(
            settings.etp_assessment_pro_vertex_service_account_filename,
            "agon.json")
        # ...but nothing was written to the persistent store.
        self.assertEqual(
            icp.get_param("etp_assessment_pro.vertex_service_account_filename"),
            "SENTINEL",
            "onchange must not persist; only set_values() may")

    def test_prompt_md_uploads_persist_on_save(self):
        """question.md / scoring.md uploads must also persist via Save."""
        icp = self._icp()
        settings = self.env["res.config.settings"].create({
            "question_prompt_upload": self._b64("Q PROMPT BODY"),
            "question_prompt_upload_filename": "question.md",
            "scoring_prompt_upload": self._b64("S PROMPT BODY"),
            "scoring_prompt_upload_filename": "scoring.md",
        })
        settings.execute()

        self.assertEqual(
            icp.get_param("etp_assessment_pro.question_prompt"), "Q PROMPT BODY")
        self.assertEqual(
            icp.get_param("etp_assessment_pro.scoring_system_prompt"),
            "S PROMPT BODY")

    def test_invalid_utf8_upload_raises(self):
        """A non-UTF-8 upload must raise a clean UserError, not a traceback."""
        from odoo.exceptions import UserError
        settings = self.env["res.config.settings"].create({
            "vertex_sa_upload": base64.b64encode(b"\xff\xfe\x00binary"),
            "vertex_sa_upload_filename": "bad.json",
        })
        with self.assertRaises(UserError):
            settings.execute()
