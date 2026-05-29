# -*- coding: utf-8 -*-
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..services import llm_qc


@tagged("post_install", "-at_install", "video_editor_s3")
class TestParseQcResponse(TransactionCase):

    def test_parses_valid_fenced_json(self):
        text = (
            "Here is my evaluation:\n\n"
            "```json\n"
            '{"score": 75, "expert_level": "advanced", "quality": "pass", '
            '"reason": "Clear and specific", "issues": []}\n'
            "```\n"
        )
        result = llm_qc._parse_qc_response(text)
        self.assertEqual(result["score"], 75.0)
        self.assertEqual(result["expert_level"], "advanced")
        self.assertEqual(result["quality"], "pass")
        self.assertEqual(result["reason"], "Clear and specific")
        self.assertEqual(result["issues"], "")

    def test_reverse_iteration_picks_last_block(self):
        text = (
            "First attempt:\n"
            "```json\n"
            '{"score": 10, "expert_level": "novice", "quality": "fail", "reason": "stub", "issues": []}\n'
            "```\n"
            "Revised:\n"
            "```json\n"
            '{"score": 90, "expert_level": "expert", "quality": "pass", "reason": "final", "issues": []}\n'
            "```\n"
        )
        result = llm_qc._parse_qc_response(text)
        self.assertEqual(result["score"], 90.0)
        self.assertEqual(result["quality"], "pass")
        self.assertEqual(result["reason"], "final")

    def test_raises_on_no_json_block(self):
        with self.assertRaises(UserError):
            llm_qc._parse_qc_response("Just plain text, no JSON.")

    def test_issues_list_joined_with_newlines(self):
        text = (
            "```json\n"
            '{"score": 40, "expert_level": "intermediate", "quality": "fail", '
            '"reason": "x", "issues": ["a", "b", "c"]}\n'
            "```\n"
        )
        result = llm_qc._parse_qc_response(text)
        self.assertIn("a", result["issues"])
        self.assertIn("b", result["issues"])
        self.assertIn("c", result["issues"])

    def test_quality_default_fail_on_invalid(self):
        text = (
            "```json\n"
            '{"score": 50, "expert_level": "intermediate", "quality": "maybe", '
            '"reason": "x", "issues": []}\n'
            "```\n"
        )
        result = llm_qc._parse_qc_response(text)
        self.assertEqual(result["quality"], "fail")


@tagged("post_install", "-at_install", "video_editor_s3")
class TestEvaluatePrompt(TransactionCase):

    def test_missing_api_key_raises(self):
        with self.assertRaises(UserError):
            llm_qc.evaluate_prompt(
                prompt="Test prompt",
                seed_prompt="You are a QC expert.",
                api_key="",
            )

    def test_empty_prompt_raises(self):
        with self.assertRaises(UserError):
            llm_qc.evaluate_prompt(
                prompt="",
                seed_prompt="You are a QC expert.",
                api_key="bedrock-api-key",
            )

    def test_evaluates_via_mocked_bedrock(self):
        fake_response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": (
                                "```json\n"
                                '{"score": 80, "expert_level": "advanced", '
                                '"quality": "pass", "reason": "Great prompt", '
                                '"issues": []}\n'
                                "```"
                            )
                        }
                    ]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50},
        }
        fake_client = MagicMock()
        fake_client.converse.return_value = fake_response

        with patch("boto3.client", return_value=fake_client):
            result = llm_qc.evaluate_prompt(
                prompt="A cinematic shot of a sunset over the ocean.",
                seed_prompt="You are a QC expert.",
                api_key="bedrock-api-key",
            )

        self.assertEqual(result["score"], 80.0)
        self.assertEqual(result["expert_level"], "advanced")
        self.assertEqual(result["quality"], "pass")
        self.assertEqual(result["reason"], "Great prompt")
        self.assertEqual(result["input_tokens"], 100)
        self.assertEqual(result["output_tokens"], 50)
        self.assertTrue(fake_client.converse.called)
