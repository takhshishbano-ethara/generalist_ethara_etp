"""Candidate-communication templates and send guards (v1.1, P1-5).

The language guard is STRUCTURAL: the rejection template is computed from
the co-signed ``block_kind`` (the sender cannot pick the letter), a
blocked candidate's letter requires a completed second-screener sign-off
(rule 8's "before it is communicated" as code), and the template wording
itself is linted here — the credibility letter carries ONLY the neutral
"unable to reconcile statements" cause sentence and zero accusation
vocabulary; the competence letter carries zero credibility vocabulary.
All sends are manual through the ``mail.compose.message`` composer.
"""

import re

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import IrisCase

#: Canonical credibility cause sentence (whitespace-normalized compare).
CREDIBILITY_SENTENCE = (
    "we were unable to reconcile statements in the application materials"
)

#: Accusation vocabulary banned from the credibility letter.
CREDIBILITY_BANNED = ("fraud", "fabricat", "dishonest", "misrepresent")

#: Credibility vocabulary banned from the competence letter.
COMPETENCE_BANNED = ("reconcile", "credib", "verif")


@tagged("post_install", "-at_install", "iris")
class TestCandidateMail(IrisCase):
    EMAIL = "jane.doe@example.com"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _hold_candidate(self, **overrides):
        overrides.setdefault("email", self.EMAIL)
        candidate = self._make_candidate(**overrides)
        self._screen(candidate, self.VALID_HOLD_RECORD)
        self.assertEqual(candidate.state, "hold")
        return candidate

    def _blocked_candidate(self, kind="credibility", **overrides):
        overrides.setdefault("email", self.EMAIL)
        candidate = self._make_candidate(**overrides)
        self._screen(candidate, self.VALID_BLOCK_RECORD)
        self.assertEqual(candidate.state, "pending_block")
        candidate.with_user(self.user_second)._block_signoff(kind)
        self.assertEqual(candidate.state, "blocked")
        return candidate

    def _template(self, xmlid):
        return self.env.ref(f"iris.{xmlid}")

    @staticmethod
    def _flat(text):
        """Lowercased, whitespace-collapsed template text for linting."""
        return re.sub(r"\s+", " ", str(text or "")).lower()

    def _template_text(self, template):
        return self._flat(template.body_html) + " " + self._flat(template.subject)

    # ------------------------------------------------------------------
    # Guards: state + email
    # ------------------------------------------------------------------
    def test_hold_email_requires_hold_state(self):
        candidate = self._make_candidate(email=self.EMAIL)
        with self.assertRaises(UserError):
            candidate.action_send_hold_verification_email()
        held = self._hold_candidate()
        action = held.action_send_hold_verification_email()
        self.assertEqual(action["res_model"], "mail.compose.message")

    def test_missing_email_blocks_every_send(self):
        candidate = self._hold_candidate(email=False)
        with self.assertRaises(UserError):
            candidate.action_send_hold_verification_email()

        blocked = self._blocked_candidate(email=False)
        with self.assertRaises(UserError):
            blocked.action_send_rejection_email()

    def test_invite_requires_shipped_or_interview_ready(self):
        candidate = self._make_candidate(email=self.EMAIL)
        with self.assertRaises(UserError):
            candidate.action_send_interview_invite_email()

        self._screen(candidate, self.VALID_SHIP_RECORD)
        action = candidate.action_send_interview_invite_email()
        self.assertEqual(
            action["context"]["default_template_id"],
            self._template("mail_template_iris_ship_invite").id,
        )

        candidate.sudo().write({"state": "interview_ready"})
        self.assertTrue(candidate.action_send_interview_invite_email())

    def test_rejection_requires_blocked_or_rejected(self):
        candidate = self._hold_candidate()
        with self.assertRaises(UserError):
            candidate.action_send_rejection_email()

    # ------------------------------------------------------------------
    # Hard gate: BLOCK communications need the completed sign-off
    # ------------------------------------------------------------------
    def test_blocked_without_approved_signoff_cannot_send(self):
        # Cron auto-block path: blocked with signoff_state='none'.
        candidate = self._hold_candidate()
        hold = candidate._get_current_hold_screening()
        hold.sudo().write({"auto_blocked": True})
        candidate.sudo().write({"state": "blocked", "hold_deadline": False})
        self.assertEqual(
            candidate.current_screening_id.block_signoff_state, "none",
        )
        with self.assertRaises(UserError):
            candidate.action_send_rejection_email()

    def test_pending_block_cannot_send_rejection(self):
        candidate = self._make_candidate(email=self.EMAIL)
        self._screen(candidate, self.VALID_BLOCK_RECORD)
        self.assertEqual(candidate.state, "pending_block")
        with self.assertRaises(UserError):
            candidate.action_send_rejection_email()

    # ------------------------------------------------------------------
    # Template resolution is computed, never picked
    # ------------------------------------------------------------------
    def test_credibility_block_resolves_credibility_template(self):
        candidate = self._blocked_candidate(kind="credibility")
        action = candidate.action_send_rejection_email()
        self.assertEqual(
            action["context"]["default_template_id"],
            self._template("mail_template_iris_reject_credibility").id,
        )

    def test_competence_block_resolves_competence_template(self):
        candidate = self._blocked_candidate(kind="competence")
        action = candidate.action_send_rejection_email()
        self.assertEqual(
            action["context"]["default_template_id"],
            self._template("mail_template_iris_reject_competence").id,
        )

    def test_rejected_state_always_maps_to_competence_template(self):
        candidate = self._make_candidate(email=self.EMAIL)
        candidate.sudo().write({"state": "rejected"})
        action = candidate.action_send_rejection_email()
        self.assertEqual(
            action["context"]["default_template_id"],
            self._template("mail_template_iris_reject_competence").id,
        )

    def test_hold_email_resolves_hold_template(self):
        candidate = self._hold_candidate()
        action = candidate.action_send_hold_verification_email()
        self.assertEqual(
            action["context"]["default_template_id"],
            self._template("mail_template_iris_hold_verification").id,
        )

    # ------------------------------------------------------------------
    # Composer plumbing (manual send only)
    # ------------------------------------------------------------------
    def test_composer_context_shape(self):
        candidate = self._blocked_candidate(kind="credibility")
        action = candidate.action_send_rejection_email()
        self.assertEqual(action["type"], "ir.actions.act_window")
        self.assertEqual(action["res_model"], "mail.compose.message")
        self.assertEqual(action["target"], "new")
        context = action["context"]
        self.assertEqual(context["default_model"], "iris.candidate")
        self.assertEqual(context["default_res_ids"], [candidate.id])
        self.assertEqual(context["default_composition_mode"], "comment")
        # No CANDIDATE-FACING mail is sent by the action itself. The
        # sign-off flow legitimately creates internal activity/notification
        # mail.mail rows, so scope the assertion to the candidate's address
        # rather than every outgoing mail on the record.
        outgoing = self.env["mail.mail"].search([
            ("model", "=", "iris.candidate"),
            ("res_id", "=", candidate.id),
            ("email_to", "ilike", candidate.email),
        ])
        self.assertFalse(outgoing)

    # ------------------------------------------------------------------
    # Language lint (CI guard on the template wording)
    # ------------------------------------------------------------------
    def test_credibility_template_language(self):
        template = self._template("mail_template_iris_reject_credibility")
        text = self._template_text(template)
        self.assertIn(CREDIBILITY_SENTENCE, text)
        for banned in CREDIBILITY_BANNED:
            self.assertNotIn(banned, text, f"banned word in template: {banned}")
        # "lie/lied/lies/lying" as words (substring check would trip on
        # innocents like "applied").
        self.assertIsNone(re.search(r"\bl(?:ie[sd]?|ying)\b", text))

    def test_competence_template_language(self):
        template = self._template("mail_template_iris_reject_competence")
        text = self._template_text(template)
        self.assertIn(
            "experience more closely matches the current requirements", text,
        )
        for banned in COMPETENCE_BANNED:
            self.assertNotIn(banned, text, f"banned word in template: {banned}")

    def test_hold_template_neutral_language(self):
        template = self._template("mail_template_iris_hold_verification")
        text = self._template_text(template)
        for banned in (
            *CREDIBILITY_BANNED, "discrepanc", "suspic", "flag",
        ):
            self.assertNotIn(banned, text, f"banned word in template: {banned}")

    # ------------------------------------------------------------------
    # HOLD template embeds the clarifying questions when present
    # ------------------------------------------------------------------
    def test_hold_template_renders_clarifying_questions(self):
        candidate = self._hold_candidate()
        hold = candidate._get_current_hold_screening()
        hold.sudo().write({
            "clarifying_questions_markdown": self.VALID_CLARIFYING_QUESTIONS,
        })
        template = self._template("mail_template_iris_hold_verification")
        rendered = template._render_field("body_html", [candidate.id])[
            candidate.id
        ]
        self.assertIn("size of the team", str(rendered))
        self.assertNotIn(
            "[INSERT VERIFICATION ITEMS FROM THE HOLD CHECKLIST]",
            str(rendered),
        )

    def test_hold_template_placeholder_without_questions(self):
        candidate = self._hold_candidate()
        self.assertFalse(
            candidate._get_current_hold_screening()
            .clarifying_questions_markdown,
        )
        template = self._template("mail_template_iris_hold_verification")
        rendered = template._render_field("body_html", [candidate.id])[
            candidate.id
        ]
        self.assertIn(
            "[INSERT VERIFICATION ITEMS FROM THE HOLD CHECKLIST]",
            str(rendered),
        )
