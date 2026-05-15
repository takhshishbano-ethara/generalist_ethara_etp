"""Pure-Python tests for ``services.scoring_service.score_prd``.

No Odoo registry is needed for these tests, but they still inherit from
``TransactionCase`` so the standard ``--test-tags leviathan`` discovery picks
them up. The actual logic is exercised through the module-level helpers.
"""
import re

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..services.scoring_service import (
    GRADE_SCALE,
    PRD_MAX_WORDS,
    PRD_MIN_WORDS,
    RUBRIC_SECTIONS,
    SECTION_MAX_POINTS,
    TIER1_BANNED_PHRASES,
    _check_font_names,
    _check_hex_codes,
    _count_words,
    _find_banned_phrases,
    _score_animations,
    _score_cool_transition,
    _score_data_model,
    _score_overall_quality,
    _score_page_breakdown,
    _score_performance,
    _score_responsive,
    _score_tech_stack,
    _score_visual_identity,
    _score_word_count_format,
    _strip_instructions,
    score_prd,
)


def _build_prd(word_count=1200, extras=""):
    """Helper: synthesise a PRD long enough to clear the word-count gate."""
    base = (
        "## Product Overview\n"
        "## Visual & Brand Direction\n"
        "Primary background: #1A1A1A and accent: #FF6600. Used for hero.\n"
        "Font: Inter, Roboto. Weight 700 SemiBold. Letter-spacing -0.02em.\n"
        "H1 | 64px, Body: 16px. 12-col grid, max-width 1280px.\n"
        "Design tokens --color-primary: defined.\n"
        "## Technical Ambition\n"
        "Next.js 14, GSAP 3.12, Sanity, Vercel for hosting. TypeScript handles types.\n"
        "## Site Architecture & Page Specifications\n"
        "### Home\n12-col grid, full-bleed hero. H1, CTA button.\n"
        "Modal opens 240ms, scale 0.95 → 1 opacity 0 → 1.\n"
        "Entry: fade in 400ms. Parallax used. Card 16:9.\n"
        "### About\n### Contact\n### Blog\n### Portfolio\n"
        "Nav header backdrop-blur 16px. Footer 4 col grid. Preloader animation.\n"
        "## Motion Language\n"
        "Default easing: cubic-bezier(0.4, 0, 0.2, 1). 240ms hero. 320ms CTA.\n"
        "180ms button. 400ms heading. 500ms modal. 600ms card.\n"
        "On scroll trigger; viewport scroll depth 40%. delay: 80ms.\n"
        "scale(1.05), translateY(-4px), opacity 0.6, rotateX(15deg), clip-path used.\n"
        "Lenis smooth scroll lerp: 0.1. Skeleton shimmer loading state.\n"
        "Forbidden: linear easing.\n"
        "## Backend & Application Logic\n"
        "Roles: Visitor, Admin, Editor. Read-only / can edit / can delete.\n"
        "Auth: OAuth + JWT, NextAuth. Session expiry redirect protected route middleware.\n"
        "## Accessibility & Quality\n"
        "Breakpoints 1440, 1024, 768, 375. 12 col → 4 col. 64px → 24px.\n"
        "Sidebar hidden on mobile. Hamburger replaces nav. prefers-reduced-motion respected.\n"
        "Touch target 44px. WCAG AA contrast ratio. Keyboard skip.to link.\n"
        "## Content & SEO\n"
        "Schema: post {title, slug, body}. Fields: title:, slug:, date:, author:, tags:.\n"
        "Types: string, boolean, datetime, reference, portableText.\n"
        "ISR revalidate webhook. Fetch via server component GraphQL.\n"
        "Lighthouse 95. LCP < 2.5s. CLS < 0.1. TBT < 200ms. AVIF, lazy load CDN.\n"
    )
    # Pad to reach requested word count
    filler_word = "specification "
    cur = _count_words(base)
    if cur < word_count:
        base = base + "\n" + (filler_word * ((word_count - cur) + 5))
    return base + extras


@tagged("post_install", "-at_install", "leviathan")
class TestScoringHelpers(TransactionCase):

    def test_count_words_strips_markdown(self):
        self.assertEqual(_count_words("# Hello world"), 2)
        self.assertEqual(_count_words("- **item** one\n- **item** two"), 4)
        self.assertEqual(_count_words("|cell|cell|cell|"), 3)

    def test_count_words_empty(self):
        self.assertEqual(_count_words(""), 0)
        self.assertEqual(_count_words("   "), 0)

    def test_find_banned_phrases_word_boundary(self):
        # Single-word phrase uses word boundary
        text = "The site has a sleek design."
        found = _find_banned_phrases(text, ["sleek", "nice"])
        self.assertIn("sleek", found)
        self.assertNotIn("nice", found)

    def test_find_banned_phrases_multi_word(self):
        text = "We want a smooth animation here."
        found = _find_banned_phrases(text, ["smooth animation"])
        self.assertIn("smooth animation", found)

    def test_find_banned_phrases_ignores_code_blocks(self):
        text = "Real prose.\n```js\nconst sleek = true;\n```\n"
        found = _find_banned_phrases(text, ["sleek"])
        self.assertEqual(found, [])

    def test_find_banned_phrases_ignores_inline_code(self):
        text = "Inline `sleek` token."
        found = _find_banned_phrases(text, ["sleek"])
        self.assertEqual(found, [])

    def test_check_hex_codes_present(self):
        result = _check_hex_codes("Primary #FF6600 and secondary #1A1A1A.")
        self.assertEqual(result["colors_with_hex"], 2)
        self.assertEqual(set(result["unique_hex_codes"]), {"#FF6600", "#1A1A1A"})

    def test_check_hex_codes_colors_without_hex(self):
        result = _check_hex_codes("The brand uses red and blue throughout.")
        self.assertEqual(result["colors_with_hex"], 0)
        self.assertGreaterEqual(result["colors_without_hex"], 1)

    def test_check_font_names_named(self):
        result = _check_font_names("font-family: Inter")
        self.assertTrue(result["has_named_fonts"])
        self.assertIn("Inter", result["named_fonts"])

    def test_check_font_names_common_fonts(self):
        result = _check_font_names("We use Roboto for headings.")
        self.assertTrue(result["has_named_fonts"])
        self.assertIn("Roboto", result["named_fonts"])

    def test_check_font_names_generic_ignored(self):
        result = _check_font_names("font-family: sans-serif")
        self.assertFalse(result["has_named_fonts"])

    def test_strip_instructions_removes_strict_rules(self):
        text = (
            "## STRICT RULES\nNever use these phrases\n"
            "## Visual & Brand Direction\nReal content."
        )
        cleaned = _strip_instructions(text)
        self.assertNotIn("STRICT RULES", cleaned)
        self.assertIn("Visual & Brand Direction", cleaned)

    def test_strip_instructions_removes_code_fences(self):
        text = "Prose here.\n```\nignored\n```\nMore prose."
        cleaned = _strip_instructions(text)
        self.assertNotIn("ignored", cleaned)
        self.assertIn("More prose", cleaned)


@tagged("post_install", "-at_install", "leviathan")
class TestScorePrdGates(TransactionCase):

    def test_below_min_words_triggers_reject(self):
        text = "Short PRD. " * 20
        result = score_prd(text)
        self.assertEqual(result["grade"], "REJECT")
        self.assertEqual(result["total_score"], 0)
        self.assertTrue(any("R4" in t for t in result["reject_triggers"]))

    def test_over_max_words_triggers_reject(self):
        text = "Word " * (PRD_MAX_WORDS + 50)
        result = score_prd(text)
        self.assertEqual(result["grade"], "REJECT")
        self.assertTrue(any("R5" in t for t in result["reject_triggers"]))

    def test_many_tier1_violations_triggers_reject(self):
        slop = " ".join(TIER1_BANNED_PHRASES[:6])
        text = _build_prd() + " " + slop
        result = score_prd(text)
        self.assertTrue(any("R1" in t for t in result["reject_triggers"]))

    def test_no_hex_codes_with_color_words_triggers_reject(self):
        text = (
            "## Visual & Brand Direction\n"
            "The site uses red, blue and green throughout.\n"
        )
        text = text + " specification " * 1200
        result = score_prd(text)
        self.assertTrue(any("R2" in t for t in result["reject_triggers"]))

    def test_no_font_names_triggers_reject(self):
        text = (
            "## Visual & Brand Direction\n"
            "Colors: #111111 #222222 #333333. No typography defined.\n"
        )
        text = text + " specification " * 1200
        result = score_prd(text)
        self.assertTrue(any("R3" in t for t in result["reject_triggers"]))

    def test_clean_prd_passes_gates(self):
        result = score_prd(_build_prd())
        self.assertEqual(result["reject_triggers"], [])
        self.assertNotEqual(result["grade"], "REJECT")


@tagged("post_install", "-at_install", "leviathan")
class TestScorePrdSections(TransactionCase):

    def test_all_sections_present(self):
        result = score_prd(_build_prd())
        for key in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S11"):
            self.assertIn(key, result["section_scores"])
            section = result["section_scores"][key]
            self.assertIn("score", section)
            self.assertIn("max", section)
            self.assertLessEqual(section["score"], section["max"])

    def test_section_max_points_consistent(self):
        for key, conf in RUBRIC_SECTIONS.items():
            self.assertEqual(SECTION_MAX_POINTS[key], conf["max_points"])

    def test_s1_word_count_in_range(self):
        result = _score_word_count_format("## H\n## H2\n## H3\n## H4\n## H5\n", 1500)
        self.assertGreater(result["score"], 0)
        self.assertLessEqual(result["score"], 5)

    def test_s1_word_count_under_min(self):
        # 700 words is in the 600-799 band → 1 point
        result = _score_word_count_format("## A\n", 700)
        self.assertGreaterEqual(result["score"], 1)

    def test_s3_no_pages_zero(self):
        result = _score_page_breakdown("nothing relevant here")
        self.assertEqual(result["score"], 0)

    def test_s4_no_motion_zero(self):
        result = _score_animations("no motion data")
        self.assertEqual(result["score"], 0)

    def test_s10_cool_transition_with_data(self):
        text = (
            "## 10. Cool Transition Addendum\n\n"
            "Transitions: home → about 600ms; about → contact 800ms; "
            "home → blog 1200ms.\n"
            "Scroll: 5% : hero. 25% | mid. 50% - peak. 75% → end. 100% : footer.\n"
            "Stagger 100ms `cubic-bezier(0.4, 0, 0.2, 1)`.\n"
            "200ms `easing` group.\n"
            "Hover: scale 1.05 background. Click: transform. Focus: opacity.\n"
            "Barba.js view-transitions-api.\n"
        )
        result = _score_cool_transition(text)
        self.assertGreater(result["score"], 0)

    def test_s10_placeholder_penalty(self):
        text = "## Cool Transition Addendum\n[Define transitions here]\n"
        result = _score_cool_transition(text)
        self.assertLessEqual(result["score"], 0)

    def test_grade_scale_covers_full_range(self):
        ranges = [(low, high) for (low, high, _g, _m) in GRADE_SCALE]
        # Every score 0-100 must fall in exactly one band
        for score in (0, 30, 59, 60, 69, 70, 79, 80, 89, 90, 100):
            hits = [(lo, hi) for (lo, hi) in ranges if lo <= score <= hi]
            self.assertEqual(len(hits), 1, f"score={score} hits={hits}")


@tagged("post_install", "-at_install", "leviathan")
class TestScorePrdGrading(TransactionCase):

    def test_cool_transition_uses_s10(self):
        result = score_prd(_build_prd(), category="Cool Transition")
        self.assertIn("S10", result["section_scores"])
        self.assertEqual(result["section_scores"]["S10"]["max"], 7)
        self.assertEqual(result["details"]["total_available"], 100)

    def test_non_cool_transition_skips_s10(self):
        result = score_prd(_build_prd(), category="Normal Website")
        self.assertEqual(result["section_scores"]["S10"]["max"], 0)
        self.assertEqual(result["details"]["total_available"], 93)

    def test_grade_assigned_when_not_rejected(self):
        result = score_prd(_build_prd())
        self.assertIn(result["grade"], {"A", "B", "C", "D", "F"})

    def test_score_within_0_100(self):
        result = score_prd(_build_prd())
        self.assertGreaterEqual(result["total_score"], 0)
        self.assertLessEqual(result["total_score"], 100)
