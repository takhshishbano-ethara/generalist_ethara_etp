import unittest
from unittest import mock

from ..services import enrichment_client


class TestBuildQCUserTurn(unittest.TestCase):

    def test_includes_all_fields(self):
        user_turn = enrichment_client.build_qc_user_turn(
            meta_prompt="You are generating a T2AV prompt for...",
            category="advertisements",
            sub_category="luxury_cars",
            topic="Porsche on Alpine switchback at midnight",
            style="casual",
            bad_sample="<|start|>We wordsWe words",
            reasons=["llm_special_token", "concat_repeat"],
            language="english",
            complexity="moderate",
        )
        self.assertIn("META_PROMPT:", user_turn)
        self.assertIn("CATEGORY: advertisements", user_turn)
        self.assertIn("SUB_CATEGORY: luxury_cars", user_turn)
        self.assertIn("TOPIC: Porsche on Alpine switchback at midnight", user_turn)
        self.assertIn("STYLE: casual", user_turn)
        self.assertIn("LANGUAGE: english", user_turn)
        self.assertIn("COMPLEXITY: moderate", user_turn)
        self.assertIn("BAD_SAMPLE", user_turn)
        self.assertIn("<|start|>We wordsWe words", user_turn)
        self.assertIn("llm_special_token, concat_repeat", user_turn)
        self.assertIn("Output ONLY the prompt text.", user_turn)

    def test_handles_missing_fields_gracefully(self):
        user_turn = enrichment_client.build_qc_user_turn(
            meta_prompt="",
            category="",
            sub_category="",
            topic="",
            style="",
            bad_sample="",
            reasons=[],
        )
        self.assertIn("(empty)", user_turn)
        self.assertIn("(unspecified)", user_turn)
        self.assertIn("(none reported)", user_turn)


class TestEnrichQC(unittest.TestCase):

    def test_missing_api_key_raises_auth_error(self):
        with self.assertRaises(enrichment_client.EnrichmentAuthError):
            enrichment_client.enrich_qc(
                openrouter_api_key="",
                meta_prompt="anything",
                category="advertisements",
                sub_category="cars",
                topic="porsche",
                style="casual",
                bad_sample="<|start|>garbage",
                reasons=["llm_special_token"],
            )

    @mock.patch.object(enrichment_client, "_enrich_via_openrouter")
    @mock.patch.object(enrichment_client, "load_qc_system_prompt", return_value="SYS")
    def test_calls_openrouter_with_qc_settings(self, _mock_sys, mock_call):
        mock_call.return_value = {
            "text": "a clean derived prompt of the porsche video",
            "input_tokens": 1200,
            "output_tokens": 35,
            "stop_reason": "stop",
            "request_id": "req-test-123",
            "served_model": "google/gemini-3.5-flash",
        }

        result = enrichment_client.enrich_qc(
            openrouter_api_key="fake-key",
            meta_prompt="full meta instruction",
            category="advertisements",
            sub_category="luxury_cars",
            topic="Porsche midnight",
            style="casual",
            bad_sample="<|start|>garbage",
            reasons=["llm_special_token"],
        )

        self.assertEqual(result["text"], "a clean derived prompt of the porsche video")
        self.assertEqual(result["request_id"], "req-test-123")

        kwargs = mock_call.call_args.kwargs
        self.assertEqual(kwargs["model_id"], enrichment_client.DEFAULT_QC_MODEL_ID)
        self.assertEqual(kwargs["temperature"], enrichment_client.DEFAULT_QC_TEMPERATURE)
        self.assertEqual(kwargs["max_tokens"], enrichment_client.DEFAULT_QC_MAX_TOKENS)
        self.assertEqual(kwargs["fallback_models"], ())
        self.assertEqual(kwargs["api_key"], "fake-key")
        self.assertEqual(kwargs["system_prompt"], "SYS")

    @mock.patch.object(enrichment_client, "_enrich_via_openrouter")
    @mock.patch.object(enrichment_client, "load_qc_system_prompt", return_value="SYS")
    def test_propagates_enrichment_error(self, _mock_sys, mock_call):
        mock_call.side_effect = enrichment_client.EnrichmentError("rate limit")
        with self.assertRaises(enrichment_client.EnrichmentError):
            enrichment_client.enrich_qc(
                openrouter_api_key="fake-key",
                meta_prompt="x",
                category="advertisements",
                sub_category="cars",
                topic="porsche",
                style="casual",
                bad_sample="<|start|>garbage",
                reasons=["llm_special_token"],
            )
