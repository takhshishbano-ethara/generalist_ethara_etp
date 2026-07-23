# -*- coding: utf-8 -*-
"""Generators chatter/activity-log feature.

The Generators screen (etp.assessment.pro.prompt) keeps a timestamped log of
user actions in its chatter, viewable by the end user beside the form. These
tests lock the two halves of that contract:
  1. the model inherits mail.thread (so it HAS a message thread + the view's
     <chatter/> has something to render); and
  2. each user action posts exactly one audit note, and per-draft approve/deny
     rolls up to the correct parent generator (even across several generators).
message_post stamps author + date itself, so we assert the note body + count,
not the timestamp.
"""
import base64

from odoo.tests.common import TransactionCase, tagged


@tagged("-at_install", "post_install")
class TestGeneratorChatterLog(TransactionCase):
    def _b64(self, text):
        return base64.b64encode(text.encode()).decode()

    def _generator(self, name="Gen"):
        # A SOP resource so action_generate_from_sop does not raise.
        return self.env["etp.assessment.pro.prompt"].create({
            "name": name,
            "resource_ids": [(0, 0, {"name": "sop.txt", "file": self._b64("hi"),
                                     "category": "sop"})],
        })

    def _mcq_draft(self, generator, name="Q1"):
        # mcq avoids the image-ready approval guard; a single keyed dimension is
        # enough for action_approve to materialize a bank question.
        return self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": generator.id,
            "name": name,
            "question_type": "mcq",
            "question_prompt": "Under the stated policy, what applies?",
            "options_json": '["Allow", "Remove"]',
            "correct_answer_json": '"Allow"',
        })

    def _notes(self, generator):
        """Audit notes only (exclude the tracking/creation subtypes)."""
        return generator.message_ids.filtered(
            lambda m: m.message_type == "comment")

    # --- structural ------------------------------------------------------
    def test_model_inherits_mail_thread(self):
        gen = self._generator()
        self.assertIn("message_ids", gen._fields,
                      "Generators must inherit mail.thread for the chatter")
        self.assertTrue(hasattr(gen, "message_post"))

    def test_form_view_has_chatter(self):
        arch = self.env.ref(
            "etp_assessment_pro.etp_assessment_pro_prompt_form").arch
        self.assertIn("<chatter", arch,
                      "the Generators form must render a chatter panel")

    # --- action logging --------------------------------------------------
    def test_generate_logs_one_note(self):
        gen = self._generator()
        before = len(self._notes(gen))
        gen.sop_question_count = 5
        gen.action_generate_from_sop()
        notes = self._notes(gen)
        self.assertEqual(len(notes) - before, 1,
                         "Generate must post exactly one audit note")
        body = notes[0].body
        self.assertIn("Generate Questions", body)
        self.assertIn("5 question(s)", body)

    def test_generate_logs_allowed_types(self):
        gen = self._generator()
        qtype = self.env["etp.assessment.pro.question.type"].search(
            [("code", "=", "mcq")], limit=1)
        if qtype:
            gen.allowed_question_type_ids = qtype
        gen.action_generate_from_sop()
        body = self._notes(gen)[0].body
        self.assertIn("Allowed types", body)
        if qtype:
            self.assertIn(qtype.name, body)

    def test_approve_all_logs_note(self):
        gen = self._generator()
        self._mcq_draft(gen, "A")
        self._mcq_draft(gen, "B")
        before = len(self._notes(gen))
        gen.action_approve_all_drafts()
        notes = self._notes(gen)
        self.assertEqual(len(notes) - before, 1,
                         "Approve All must post exactly one rollup note, "
                         "not one per draft and not a duplicate summary")
        self.assertIn("approved", notes[0].body)
        self.assertIn("2 draft(s)", notes[0].body)

    def test_per_draft_approve_logs_to_parent(self):
        gen = self._generator()
        d1 = self._mcq_draft(gen, "One")
        d2 = self._mcq_draft(gen, "Two")
        before = len(self._notes(gen))
        (d1 | d2).action_approve()
        notes = self._notes(gen)
        self.assertEqual(len(notes) - before, 1,
                         "one summary note per generator, not per draft")
        self.assertIn("approved", notes[0].body)
        self.assertIn("One", notes[0].body)
        self.assertIn("Two", notes[0].body)

    def test_per_draft_deny_logs_to_parent(self):
        gen = self._generator()
        d1 = self._mcq_draft(gen, "Deny me")
        before = len(self._notes(gen))
        d1.action_deny()
        notes = self._notes(gen)
        self.assertEqual(len(notes) - before, 1)
        self.assertIn("denied", notes[0].body)
        self.assertIn("Deny me", notes[0].body)

    def test_approve_across_generators_logs_each(self):
        g1 = self._generator("G1")
        g2 = self._generator("G2")
        d1 = self._mcq_draft(g1, "from-g1")
        d2 = self._mcq_draft(g2, "from-g2")
        b1, b2 = len(self._notes(g1)), len(self._notes(g2))
        (d1 | d2).action_approve()
        self.assertEqual(len(self._notes(g1)) - b1, 1)
        self.assertEqual(len(self._notes(g2)) - b2, 1)
        self.assertIn("from-g1", self._notes(g1)[0].body)
        self.assertIn("from-g2", self._notes(g2)[0].body)
        # No cross-contamination between generators.
        self.assertNotIn("from-g2", self._notes(g1)[0].body)

    def test_log_activity_is_best_effort(self):
        """A logging failure must never propagate out of the action."""
        gen = self._generator()
        # An unsaved (NewId) record has no id; _log_activity must skip silently.
        virtual = self.env["etp.assessment.pro.prompt"].new({"name": "v"})
        virtual._log_activity("noop")  # must not raise

    def test_deny_empty_selection_no_note(self):
        gen = self._generator()
        d1 = self._mcq_draft(gen, "already")
        d1.action_deny()
        before = len(self._notes(gen))
        # Denying an already-denied draft filters to empty -> no new note.
        d1.action_deny()
        self.assertEqual(len(self._notes(gen)), before,
                         "no audit note when nothing actually changed")

    # --- Approve All skip-and-continue -----------------------------------
    def _imageless_image_draft(self, generator, name="NoPic"):
        """An image_prompt draft with no rendered image: action_approve's
        image-ready guard rejects it, so it is a deterministic per-draft
        failure for the skip-and-continue path (no need to fake flaw JSON)."""
        return self.env["etp.assessment.pro.prompt.question"].create({
            "prompt_id": generator.id,
            "name": name,
            "question_type": "image_prompt",
            "question_prompt": "Rewrite the prompt to fix the artifact.",
        })

    def test_approve_all_skips_bad_draft_keeps_good(self):
        """One bad draft must not roll back the good ones (skip-and-continue)."""
        gen = self._generator()
        good1 = self._mcq_draft(gen, "GoodOne")
        good2 = self._mcq_draft(gen, "GoodTwo")
        bad = self._imageless_image_draft(gen, "BadNoImage")
        gen.action_approve_all_drafts()
        self.assertEqual(good1.state, "approved")
        self.assertEqual(good2.state, "approved")
        self.assertTrue(good1.approved_question_id)
        self.assertTrue(good2.approved_question_id)
        # The bad draft stayed a draft (rolled back), never a half-approved row.
        self.assertEqual(bad.state, "draft")
        self.assertFalse(bad.approved_question_id)

    def test_approve_all_partial_logs_approved_and_skip(self):
        """Partial approval posts the approved rollup AND a skip note naming
        exactly what was skipped."""
        gen = self._generator()
        self._mcq_draft(gen, "KeepMe")
        self._imageless_image_draft(gen, "SkipMe")
        before = len(self._notes(gen))
        gen.action_approve_all_drafts()
        bodies = " ".join(n.body for n in self._notes(gen)[:len(self._notes(gen)) - before])
        # approved rollup names the good draft, skip note names the bad one.
        self.assertIn("KeepMe", bodies)
        self.assertIn("SkipMe", bodies)
        self.assertIn("skipped", bodies)

    def test_approve_all_all_bad_raises_with_reasons(self):
        """If every draft fails, surface a UserError with the reasons rather
        than silently approving nothing."""
        from odoo.exceptions import UserError
        gen = self._generator()
        self._imageless_image_draft(gen, "OnlyBad")
        with self.assertRaises(UserError):
            gen.action_approve_all_drafts()

    def test_approve_all_clean_set_still_one_note(self):
        """The all-good path is unchanged: exactly one rollup note, return True."""
        gen = self._generator()
        self._mcq_draft(gen, "A")
        self._mcq_draft(gen, "B")
        before = len(self._notes(gen))
        result = gen.action_approve_all_drafts()
        self.assertIs(result, True)
        notes = self._notes(gen)
        self.assertEqual(len(notes) - before, 1,
                         "clean Approve All still posts exactly one rollup note")
        self.assertIn("2 draft(s)", notes[0].body)
