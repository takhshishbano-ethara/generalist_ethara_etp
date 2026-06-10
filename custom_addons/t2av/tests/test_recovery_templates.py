import unittest
from types import SimpleNamespace

from ..services import recovery_templates
from ..services.ambiguity_detector import detect_ambiguity


def _rec(**kwargs):
    base = dict(
        category="general", style="casual", topic="a clean subject",
        sub_category="general subject", duration=10,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestLayerA(unittest.TestCase):

    def test_known_combo_uses_exact_template(self):
        rec = _rec(
            category="av_sync_sound_effects", style="casual",
            topic="a soft hand-clap", sub_category="hands clapping",
        )
        out = recovery_templates.tier2_template_fallback(rec)
        self.assertIn("a soft hand-clap", out)
        self.assertIn("hands clapping", out)
        self.assertTrue(out.startswith("A 10-second"))


class TestLayerB(unittest.TestCase):

    def test_unknown_style_falls_back_to_category_casual(self):
        rec = _rec(
            category="av_sync_sound_effects",
            style="never_existed_style",
            topic="a soft hand-clap", sub_category="hands clapping",
        )
        out = recovery_templates.tier2_template_fallback(rec)
        self.assertIn("a soft hand-clap", out)
        self.assertIn("hands clapping", out)


class TestLayerC(unittest.TestCase):

    def test_unknown_category_uses_safety_net(self):
        rec = _rec(
            category="future_category_xyz",
            style="casual",
            topic="a brand-new subject",
            sub_category="some sub",
        )
        out = recovery_templates.tier2_template_fallback(rec)
        self.assertIn("a brand-new subject", out)
        self.assertIn("some sub", out)
        self.assertIn("future_category_xyz", out)

    def test_safety_net_long_band_extension(self):
        rec = _rec(
            category="future_category_xyz",
            style="precise",
            topic="a brand-new subject",
            sub_category="some sub",
        )
        out = recovery_templates.tier2_template_fallback(rec)
        self.assertGreater(len(out.split()), 60)

    def test_missing_fields_default_safely(self):
        rec = _rec(
            category="", style="", topic="", sub_category="", duration=None,
        )
        out = recovery_templates.tier2_template_fallback(rec)
        self.assertTrue(out)
        self.assertGreater(len(out), 20)


class TestSelfConsistency(unittest.TestCase):

    def test_every_layer_a_template_passes_detector(self):
        for (category, style), _template in recovery_templates.TEMPLATES.items():
            rec = _rec(
                category=category, style=style,
                topic="a porsche carving an alpine switchback at midnight",
                sub_category="luxury sports car",
                duration=10,
            )
            out = recovery_templates.tier2_template_fallback(rec)
            result = detect_ambiguity(out)
            self.assertFalse(
                result["is_ambiguous"],
                f"Tier 2 template for {category}/{style} produced "
                f"ambiguous output. Reasons: {result['reasons']}. "
                f"Output: {out[:120]!r}",
            )

    def test_safety_net_passes_detector_across_styles(self):
        styles = ("casual", "precise", "narrative", "terse", "exhaustive", "creative")
        for style in styles:
            rec = _rec(
                category="future_unknown",
                style=style,
                topic="a porsche carving an alpine switchback at midnight",
                sub_category="luxury sports car",
                duration=10,
            )
            out = recovery_templates.tier2_template_fallback(rec)
            result = detect_ambiguity(out)
            self.assertFalse(
                result["is_ambiguous"],
                f"Safety net for style={style} produced ambiguous output. "
                f"Reasons: {result['reasons']}. Output: {out[:120]!r}",
            )
