from unittest import mock

from odoo.tests.common import TransactionCase


_BAD_PROMPT = "<|start|>We wordsWe words We words We words We words"
_CLEAN_PROMPT = (
    "yo make a video of a midnight-blue porsche carving an alpine "
    "switchback as dawn light cuts across the hood"
)


class TestAmbiguityRecoveryE2E(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Gen = cls.env["t2av.generation"]
        cls.env["ir.config_parameter"].sudo().set_param(
            "t2av.ambiguity_recovery_enabled", "True",
        )

    def _make(self, **overrides):
        vals = {
            "prompt": _BAD_PROMPT,
            "raw_prompt": _BAD_PROMPT,
            "meta_prompt": "You are generating a T2AV prompt for a porsche video.",
            "category": "high_motion_action",
            "sub_category": "luxury sports car",
            "topic": "porsche alpine switchback midnight",
            "style": "casual",
            "language": "english",
            "complexity": "moderate",
            "source": "import",
        }
        vals.update(overrides)
        return self.Gen.create(vals)

    def test_clean_prompt_skips_recovery(self):
        rec = self._make(prompt=_CLEAN_PROMPT, raw_prompt=_CLEAN_PROMPT)
        rec._t2av_run_ambiguity_recovery_pass()
        self.assertFalse(rec.ambiguity_detected)
        self.assertFalse(rec.recovery_used)
        self.assertEqual(rec.recovery_tier, "none")
        self.assertEqual(rec.prompt, _CLEAN_PROMPT)

    def test_feature_flag_off_skips_recovery(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "t2av.ambiguity_recovery_enabled", "False",
        )
        try:
            rec = self._make()
            rec._t2av_run_ambiguity_recovery_pass()
            self.assertFalse(rec.recovery_used)
            self.assertEqual(rec.prompt, _BAD_PROMPT)
        finally:
            self.env["ir.config_parameter"].sudo().set_param(
                "t2av.ambiguity_recovery_enabled", "True",
            )

    def test_idempotent_when_already_recovered(self):
        rec = self._make(recovery_used=True, recovery_tier="tier1")
        rec._t2av_run_ambiguity_recovery_pass()
        self.assertEqual(rec.prompt, _BAD_PROMPT)

    def test_no_meta_prompt_skips_recovery(self):
        rec = self._make(meta_prompt="")
        rec._t2av_run_ambiguity_recovery_pass()
        self.assertTrue(rec.ambiguity_detected)
        self.assertFalse(rec.recovery_used)
        self.assertEqual(rec.prompt, _BAD_PROMPT)

    @mock.patch(
        "odoo.addons.t2av.models.credential_manager.get_openrouter_api_key",
        return_value="fake-key",
    )
    def test_tier1_success_overwrites_prompt(self, _mock_key):
        with mock.patch(
            "odoo.addons.t2av.services.enrichment_client.enrich_qc",
            return_value={
                "text": _CLEAN_PROMPT,
                "stop_reason": "stop",
                "input_tokens": 1200,
                "output_tokens": 35,
                "request_id": "req-1",
                "served_model": "google/gemini-3.5-flash",
            },
        ):
            rec = self._make()
            rec._t2av_run_ambiguity_recovery_pass()
        self.assertTrue(rec.ambiguity_detected)
        self.assertTrue(rec.recovery_used)
        self.assertEqual(rec.recovery_tier, "tier1")
        self.assertEqual(rec.prompt, _CLEAN_PROMPT)
        self.assertEqual(rec.raw_prompt, _BAD_PROMPT)

    @mock.patch(
        "odoo.addons.t2av.models.credential_manager.get_openrouter_api_key",
        return_value="fake-key",
    )
    def test_tier1_garbage_output_falls_to_tier2(self, _mock_key):
        with mock.patch(
            "odoo.addons.t2av.services.enrichment_client.enrich_qc",
            return_value={
                "text": "<|start|>also garbage also garbage also garbage",
                "stop_reason": "stop",
                "input_tokens": 1200,
                "output_tokens": 5,
                "request_id": "req-2",
                "served_model": "google/gemini-3.5-flash",
            },
        ):
            rec = self._make()
            rec._t2av_run_ambiguity_recovery_pass()
        self.assertTrue(rec.recovery_used)
        self.assertEqual(rec.recovery_tier, "tier2")
        self.assertNotEqual(rec.prompt, _BAD_PROMPT)
        self.assertNotIn("<|start|>", rec.prompt)
        self.assertEqual(rec.raw_prompt, _BAD_PROMPT)

    @mock.patch(
        "odoo.addons.t2av.models.credential_manager.get_openrouter_api_key",
        return_value="fake-key",
    )
    def test_tier1_llm_error_falls_to_tier2(self, _mock_key):
        from odoo.addons.t2av.services import enrichment_client
        with mock.patch(
            "odoo.addons.t2av.services.enrichment_client.enrich_qc",
            side_effect=enrichment_client.EnrichmentError("rate limit"),
        ):
            rec = self._make()
            rec._t2av_run_ambiguity_recovery_pass()
        self.assertEqual(rec.recovery_tier, "tier2")
        self.assertNotEqual(rec.prompt, _BAD_PROMPT)

    @mock.patch(
        "odoo.addons.t2av.models.credential_manager.get_openrouter_api_key",
        return_value="",
    )
    def test_missing_api_key_falls_to_tier2(self, _mock_key):
        rec = self._make()
        rec._t2av_run_ambiguity_recovery_pass()
        self.assertEqual(rec.recovery_tier, "tier2")
        self.assertNotEqual(rec.prompt, _BAD_PROMPT)
