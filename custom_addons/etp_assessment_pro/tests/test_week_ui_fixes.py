# -*- coding: utf-8 -*-
"""Tests for the week's UI fixes: candidate feedback in Review Answers (g4) and
tag machine-key auto-population + validation (g5)."""
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged("-at_install", "post_install")
class TestTagMachineKeyAutoPopulate(TransactionCase):
    """g5: a human adds a tag by typing Prefix + Readable Name; the machine key
    builds itself in canonical form, and bad input is rejected."""

    # Fixed keys these tests mint. A real generation run (SOP tag extraction)
    # can leave any of these committed in a shared dev DB, which would trip the
    # one-key-one-tag uniqueness guard on create. Clear them in setUp so the
    # test owns a clean slate; TransactionCase rolls the delete back afterwards,
    # so committed data in CI/prod is never actually touched.
    _MINTED_KEYS = (
        "skill:quality-prompt-writing", "domain:ai-image-editing",
        "task:evaluate-quality", "skill:evaluate-quality", "modality:video",
    )

    def setUp(self):
        super().setUp()
        self.env["etp.assessment.pro.tag"].search(
            [("name", "in", list(self._MINTED_KEYS))]).unlink()

    def _Tag(self):
        return self.env["etp.assessment.pro.tag"]

    def test_prefix_plus_display_builds_canonical_key(self):
        tag = self._Tag().create({
            "prefix": "skill",
            "display": "Quality Prompt Writing",
        })
        self.assertEqual(tag.name, "skill:quality-prompt-writing")
        self.assertEqual(tag.prefix, "skill")
        self.assertEqual(tag.label, "quality-prompt-writing")

    def test_llm_path_name_only_derives_prefix(self):
        # The extraction path writes name directly; prefix must be derived.
        tag = self._Tag().create({"name": "domain:ai-image-editing",
                                   "display": "AI Image Editing"})
        self.assertEqual(tag.prefix, "domain")
        self.assertEqual(tag.name, "domain:ai-image-editing")

    def test_uppercase_spaces_in_name_are_canonicalized(self):
        tag = self._Tag().create({"name": "Domain: AI Image Editing"})
        # canonical: lowercase, spaces->hyphens, one colon
        self.assertEqual(tag.name, "domain:ai-image-editing")

    def test_unknown_prefix_rejected(self):
        with self.assertRaises(ValidationError):
            self._Tag().create({"prefix": "bogus", "display": "Something"})

    def test_write_prefix_rebuilds_key(self):
        tag = self._Tag().create({"prefix": "task", "display": "Evaluate Quality"})
        self.assertEqual(tag.name, "task:evaluate-quality")
        # Changing the FACET re-keys (the value is preserved, prefix swapped).
        tag.write({"prefix": "skill"})
        self.assertEqual(tag.name, "skill:evaluate-quality")

    def test_display_pin_update_preserves_existing_key(self):
        # The LLM _get_or_create path updates `display` to pin a readable name to
        # an existing key; that must NOT re-mint the machine key (one-key-one-
        # display invariant the ranker depends on).
        tag = self._Tag().create({"name": "domain:ai-image-editing"})
        tag.write({"display": "Picture Editing"})
        self.assertEqual(tag.name, "domain:ai-image-editing")
        self.assertEqual(tag.display, "Picture Editing")

    def test_recolor_does_not_touch_key(self):
        tag = self._Tag().create({"prefix": "modality", "display": "Video"})
        key_before = tag.name
        tag.write({"color": 5})
        self.assertEqual(tag.name, key_before)


@tagged("-at_install", "post_install")
class TestCandidateFeedbackInReview(TransactionCase):
    """g4: released subjective answers surface the grader's llm_feedback in the
    Review Answers rows (was computed + stored but never passed to the template).
    Locks the row-shape contract so a refactor that drops the field breaks loudly."""

    def test_response_model_carries_feedback_fields(self):
        Resp = self.env["etp.assessment.pro.response"]
        self.assertIn("llm_feedback", Resp._fields)
        self.assertIn("llm_state", Resp._fields)

    def test_review_row_builder_emits_feedback_key(self):
        # _build_answer_review is a controller method; assert it references the
        # feedback field (regression guard against silently dropping it again).
        import inspect
        from odoo.addons.etp_assessment_pro.controllers import portal
        src = inspect.getsource(portal.EtpAssessmentPortal._build_answer_review)
        self.assertIn("llm_feedback", src)
        self.assertIn('"feedback"', src)

