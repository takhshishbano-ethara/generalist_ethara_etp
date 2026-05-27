from odoo.tests.common import BaseCase, tagged

from odoo.addons.t2av.services import (
    auto_repair,
    template_fallback,
    validator as validator_svc,
)


_CATEGORIES = (
    "animals_wildlife",
    "food",
    "cars",
    "fashion",
    "sports",
    "nature_landscape",
    "urban_street",
    "indoor_lifestyle",
    "multi_speaker_dialogue",
)

_STYLES = ("casual", "precise", "narrative", "terse", "exhaustive", "creative")

_STYLE_BANDS = {
    "casual": (150, 220),
    "precise": (180, 240),
    "narrative": (200, 280),
    "terse": (80, 140),
    "exhaustive": (240, 280),
    "creative": (180, 240),
}


def _metadata(category, topic="the subject", prompt=""):
    return {
        "Category": category,
        "Sub_Category": "",
        "Style": "Precise",
        "Priority": "Medium",
        "Topic": topic,
        "Complexity": "Moderate",
        "Prompt": prompt,
    }


def _validate(text, *, style, category):
    report = validator_svc.validate(text, style=style, category=category)
    return validator_svc.categorize(report), report


@tagged("post_install", "-at_install", "t2av")
class TestTemplateFallback(BaseCase):

    def test_all_categories_all_styles_non_fatal(self):
        failures = []
        for category in _CATEGORIES:
            for style in _STYLES:
                meta = _metadata(category, topic=f"{category} subject")
                try:
                    text = template_fallback.build(meta, style=style)
                except Exception as e:
                    failures.append(f"{category}/{style}: build crash {e!r}")
                    continue
                if auto_repair.MANDATORY_SUFFIX_STEREO not in text:
                    failures.append(
                        f"{category}/{style}: missing mandatory suffix"
                    )
                    continue
                bucket, report = _validate(text, style=style, category=category)
                if bucket == "fatal":
                    fatals = [
                        f"{f.rule}({f.evidence})" for f in report.fatal[:5]
                    ]
                    failures.append(
                        f"{category}/{style}: FATAL -> {fatals}"
                    )
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_word_counts_inside_style_band(self):
        failures = []
        for category in _CATEGORIES:
            for style, (lo, hi) in _STYLE_BANDS.items():
                meta = _metadata(category)
                text = template_fallback.build(meta, style=style)
                wc = len([w for w in text.split() if w.strip()])
                if wc < lo or wc > hi:
                    failures.append(
                        f"{category}/{style}: word_count={wc} outside band {lo}-{hi}"
                    )
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_drone_topic_sanitised(self):
        meta = _metadata(
            "animals_wildlife",
            topic="drone aerial footage of a tiger",
            prompt="show the drone capturing the scene",
        )
        text = template_fallback.build(meta, style="precise")
        lower = text.lower()
        self.assertNotIn("drone", lower)
        self.assertNotIn("aerial", lower)
        bucket, _r = _validate(
            text, style="precise", category="animals_wildlife",
        )
        self.assertNotEqual(bucket, "fatal")

    def test_brand_topic_sanitised(self):
        meta = _metadata(
            "fashion",
            topic="a Nike model at Times Square",
        )
        text = template_fallback.build(meta, style="precise")
        lower = text.lower()
        self.assertNotIn("nike", lower)
        self.assertNotIn("times square", lower)
        bucket, _r = _validate(text, style="precise", category="fashion")
        self.assertNotEqual(bucket, "fatal")

    def test_unknown_category_uses_default(self):
        meta = _metadata("__nonexistent_category__")
        text = template_fallback.build(meta, style="precise")
        self.assertIn(auto_repair.MANDATORY_SUFFIX_STEREO, text)
        bucket, _r = _validate(
            text, style="precise", category="__nonexistent_category__",
        )
        self.assertNotEqual(bucket, "fatal")

    def test_empty_metadata_does_not_crash(self):
        text = template_fallback.build({}, style="precise")
        self.assertIn(auto_repair.MANDATORY_SUFFIX_STEREO, text)
        bucket, _r = _validate(text, style="precise", category="")
        self.assertNotEqual(bucket, "fatal")
