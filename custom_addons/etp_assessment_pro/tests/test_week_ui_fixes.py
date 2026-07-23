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

    def test_candidate_feedback_is_verdict_never_internal_reasoning(self):
        """The candidate-facing llm_feedback MUST be the clean judge `verdict`,
        NEVER `reasoning` — the composition lanes + ceiling path stuff the INTERNAL
        scoring math (e.g. '[image_label coverage=0.33 x correctness=0.00 -> raw
        0]') into `reasoning`, and that is admin-only. Regression for the bug where
        _store_scored fell back feedback -> reasoning and leaked the math to the
        candidate's Review Answers 'Grader feedback' box."""
        import inspect
        from odoo.addons.etp_assessment_pro.services import scoring
        src = inspect.getsource(scoring._store_scored)
        # the feedback source line must prefer verdict and must NOT fall back to
        # reasoning (which carries the internal composition/ceiling audit).
        self.assertIn('it.get("feedback") or it.get("verdict")', src)
        self.assertNotIn('it.get("feedback") or it.get("reasoning")', src)

    def test_score_breakdown_does_not_duplicate_verdict(self):
        # The verdict shows once (Grader feedback box). The Score breakdown header
        # must not re-print score_verdict, or the candidate sees it twice.
        import os
        from odoo.addons.etp_assessment_pro import __path__ as modpath
        tmpl = os.path.join(modpath[0], "views", "portal_templates.xml")
        with open(tmpl, encoding="utf-8") as fh:
            xml = fh.read()
        self.assertNotIn("Score breakdown<t t-if=\"row.get('score_verdict')\"", xml)

