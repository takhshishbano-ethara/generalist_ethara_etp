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
