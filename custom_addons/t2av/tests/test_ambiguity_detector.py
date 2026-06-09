import csv
import os
import unittest

from odoo.tests.common import TransactionCase

from ..services.ambiguity_detector import detect_ambiguity

_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GARBAGE_CSV = os.path.join(_MODULE_ROOT, "sheet7_garbage_FINAL.csv")


class TestAmbiguityDetectorPatterns(unittest.TestCase):

    def test_empty_prompt_flagged(self):
        result = detect_ambiguity("")
        self.assertTrue(result["is_ambiguous"])
        self.assertIn("empty", result["reasons"])
        self.assertEqual(result["confidence"], 1.0)

    def test_whitespace_only_flagged(self):
        result = detect_ambiguity("   \n\t  ")
        self.assertTrue(result["is_ambiguous"])
        self.assertIn("empty", result["reasons"])

    def test_llm_special_tokens_flagged(self):
        for token in ("<|start|>", "<|eom|>", "<|im_start|>", "<|assistant|>"):
            result = detect_ambiguity(f"{token}We words We words We words")
            self.assertTrue(result["is_ambiguous"], f"Token {token} not flagged")
            self.assertIn("llm_special_token", result["reasons"])

    def test_chat_template_markers_flagged(self):
        for marker in ("<s>", "[INST]", "<bos>"):
            result = detect_ambiguity(f"{marker} make a video of a cat playing")
            self.assertTrue(result["is_ambiguous"], f"Marker {marker} not flagged")
            self.assertIn("chat_template_marker", result["reasons"])

    def test_to_self_marker_flagged(self):
        result = detect_ambiguity("to=selfselfAToWeWeTheassistantassistant")
        self.assertTrue(result["is_ambiguous"])
        self.assertIn("to_self_marker", result["reasons"])

    def test_assistant_loop_flagged(self):
        result = detect_ambiguity("hello assistant assistant assistant assistant world")
        self.assertTrue(result["is_ambiguous"])
        self.assertIn("assistant_loop", result["reasons"])

    def test_concat_repeat_flagged(self):
        result = detect_ambiguity("TARGET" * 50)
        self.assertTrue(result["is_ambiguous"])
        self.assertIn("concat_repeat", result["reasons"])

    def test_concat_repeat_with_separators_flagged(self):
        result = detect_ambiguity("We We We We We words")
        self.assertTrue(result["is_ambiguous"])
        self.assertIn("concat_repeat", result["reasons"])

    def test_too_short_flagged(self):
        result = detect_ambiguity("short text")
        self.assertTrue(result["is_ambiguous"])
        self.assertIn("too_short", result["reasons"])

    def test_runaway_length_flagged(self):
        long = "the quick brown fox jumps over the lazy dog " * 60
        result = detect_ambiguity(long)
        self.assertTrue(result["is_ambiguous"])
        self.assertIn("runaway_length", result["reasons"])

    def test_meta_leak_flagged(self):
        result = detect_ambiguity(
            "You are generating a T2AV prompt for category advertisements"
        )
        self.assertTrue(result["is_ambiguous"])
        self.assertIn("meta_leak", result["reasons"])

    def test_topic_irrelevance_flagged_when_no_overlap(self):
        result = detect_ambiguity(
            "yo make a video of a cat playing piano in the kitchen on a sunny day",
            topic="Porsche Alpine switchback midnight",
        )
        self.assertIn("topic_irrelevance", result["reasons"])

    def test_topic_relevance_not_flagged_when_overlap(self):
        result = detect_ambiguity(
            "yo make a video of a porsche carving an alpine switchback at midnight",
            topic="Porsche Alpine switchback midnight",
        )
        self.assertNotIn("topic_irrelevance", result["reasons"])

    def test_language_mismatch_flagged_when_non_latin_with_english_expected(self):
        result = detect_ambiguity(
            "请生成一个视频展示猫咪在厨房里弹钢琴的场景演奏美妙的音乐场景",
            language="english",
        )
        self.assertIn("language_mismatch", result["reasons"])

    def test_language_mismatch_not_flagged_when_english(self):
        result = detect_ambiguity(
            "yo make a video of a cat playing piano in the kitchen",
            language="english",
        )
        self.assertNotIn("language_mismatch", result["reasons"])

    def test_clean_prompt_not_flagged(self):
        clean = (
            "yo can you make a video of like a midnight-blue porsche carving "
            "an alpine switchback with the dawn light cutting across the hood"
        )
        result = detect_ambiguity(clean)
        self.assertFalse(result["is_ambiguous"])
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["confidence"], 0.0)

    def test_high_vs_low_confidence_split(self):
        result = detect_ambiguity("<|start|>We wordsWe words We words We words We")
        self.assertIn("llm_special_token", result["high_confidence_signals"])
        self.assertGreaterEqual(result["confidence"], 0.7)

    def test_low_confidence_only_capped_at_0_6(self):
        result = detect_ambiguity(
            "yo make a video of a cat in a kitchen playing some music",
            topic="Porsche Alpine switchback midnight",
        )
        if result["low_confidence_signals"] and not result["high_confidence_signals"]:
            self.assertLessEqual(result["confidence"], 0.60)


class TestAmbiguityDetectorAgainstGroundTruth(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._has_garbage_csv = os.path.exists(_GARBAGE_CSV)

    def test_garbage_csv_100_percent_recall(self):
        if not self._has_garbage_csv:
            self.skipTest(f"Ground-truth CSV not found at {_GARBAGE_CSV}")

        misses = []
        total = 0
        with open(_GARBAGE_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bad = row.get("Prompt") or ""
                if not bad.strip():
                    continue
                total += 1
                result = detect_ambiguity(bad)
                if not result["is_ambiguous"]:
                    misses.append({
                        "excel_row": row.get("Excel_Row", "?"),
                        "id": row.get("ID", "?"),
                        "expected_reasons": row.get("Reasons", ""),
                        "sample": bad[:80],
                    })

        recall = (total - len(misses)) / max(total, 1)
        self.assertGreaterEqual(
            recall,
            1.0,
            f"Detector recall {recall:.2%} on {total} garbage rows. "
            f"Missed {len(misses)}: {misses[:5]}",
        )
