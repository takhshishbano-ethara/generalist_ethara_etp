"""Iris screening — one LLM forensic screen of a candidate resume.

Each row is one screening attempt. Re-screens (after HOLD verification
evidence is recorded) chain to their parent via ``parent_screening_id`` and
feed the evidence + the prior HOLD record back into the prompt.

Verdict side effects on the candidate (shared by the LLM parse path and the
manager manual-verdict path through :meth:`_apply_verdict`):

* ship  → candidate ``shipped``, hold episode cleared
* block → candidate ``pending_block``: the screening keeps the verdict and
  ``block_signoff_state`` goes ``pending``; the candidate is only finally
  ``blocked`` after a SECOND screener co-signs
  (``candidate._block_signoff``). ``hold_deadline`` is deliberately NOT
  cleared here — only the confirmed co-sign clears it, so a reject→HOLD
  cycle keeps the never-extended deadline invariant. The daily auto-block
  cron is structurally EXEMPT (it writes ``state`` directly on
  ``hold``-state candidates and never passes through here).
* hold  → candidate ``hold``; ``hold_deadline`` set ONLY on the first hold
  of the episode (re-HOLD keeps the original deadline)
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import business_days, prompt_loader, prompt_sanitizer, verdict_parser

_logger = logging.getLogger(__name__)

_VERDICT_LABELS = {"ship": "✅ SHIP", "hold": "⏸ HOLD", "block": "🚫 BLOCK"}


class IrisScreening(models.Model):
    _name = "iris.screening"
    _description = "Iris Screening"
    _inherit = ["iris.llm.job.mixin", "mail.thread"]
    _order = "id desc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    candidate_id = fields.Many2one(
        "iris.candidate", string="Candidate",
        required=True, ondelete="cascade", index=True,
    )
    attempt = fields.Integer(
        string="Attempt", readonly=True,
        help="1-based screening attempt number for this candidate.",
    )
    is_rescreen = fields.Boolean(string="Is Re-screen", readonly=True)
    parent_screening_id = fields.Many2one(
        "iris.screening", string="Parent Screening", readonly=True,
        help="The HOLD screening this re-screen resolves.",
    )
    verdict = fields.Selection(
        selection=[("ship", "Ship"), ("hold", "Hold"), ("block", "Block")],
        string="Verdict", copy=False, index=True,
    )
    verdict_manual = fields.Boolean(
        string="Manual Verdict", copy=False,
        help="Set when a manager resolved this screening from Needs Review.",
    )
    markdown_record = fields.Text(string="Screening Record (Markdown)", copy=False)
    markdown_html = fields.Html(
        string="Screening Record", compute="_compute_markdown_html",
        sanitize=False,
    )
    screened_at = fields.Datetime(string="Screened At", copy=False)
    hold_deadline = fields.Date(
        string="Hold Deadline", copy=False,
        help="Snapshot of the candidate's effective hold deadline when this "
             "screening produced a HOLD verdict.",
    )
    verification_evidence = fields.Text(string="Verification Evidence", copy=False)
    evidence_recorded_by = fields.Many2one(
        "res.users", string="Evidence Recorded By", copy=False,
    )
    evidence_recorded_at = fields.Datetime(string="Evidence Recorded At", copy=False)
    auto_blocked = fields.Boolean(
        string="Auto-Blocked", copy=False,
        help="Set when the daily cron closed this HOLD as BLOCK after the "
             "deadline expired.",
    )
    attachment_id = fields.Many2one(
        "ir.attachment", string="Record Attachment", copy=False,
    )
    batch_id = fields.Many2one(
        "iris.screening.batch", string="Batch",
        related="candidate_id.batch_id", store=True, index=True,
        help="Batch the candidate belongs to (stored related — feeds the "
             "batch's screening_ids and the consistency cost rollup).",
    )
    rescreen_reason = fields.Selection(
        selection=[
            ("hold_evidence", "HOLD Verification Evidence"),
            ("batch_consistency", "Batch Consistency Advisory"),
        ],
        string="Re-screen Reason", readonly=True, copy=False,
        help="Why this re-screen was triggered: recorded HOLD verification "
             "evidence, or an advisory finding from a batch consistency "
             "review.",
    )
    requested_by_id = fields.Many2one(
        "res.users", string="Requested By", readonly=True, copy=False,
        help="User who triggered this screening run — the \"first "
             "screener\" for an LLM-proposed BLOCK. Recorded explicitly "
             "(create_uid is unreliable under sudo'd controllers); a retry "
             "re-owns the run.",
    )
    block_proposed_by_id = fields.Many2one(
        "res.users", string="BLOCK Proposed By", readonly=True, copy=False,
        help="LLM path: the user who requested the screening. Manual path: "
             "the manager who set the verdict.",
    )
    block_proposed_at = fields.Datetime(
        string="BLOCK Proposed At", readonly=True, copy=False,
    )
    block_signoff_state = fields.Selection(
        selection=[
            ("none", "None"),
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="BLOCK Sign-off", default="none", copy=False, index=True,
        help="Dual-control state for human-visible BLOCKs: the verdict "
             "lands on the screening immediately, but the candidate stays "
             "in Pending BLOCK Sign-off until a different screener "
             "co-signs (cron auto-blocks are exempt by design).",
    )
    block_signed_off_by_id = fields.Many2one(
        "res.users", string="BLOCK Signed Off By", readonly=True, copy=False,
    )
    block_signed_off_at = fields.Datetime(
        string="BLOCK Signed Off At", readonly=True, copy=False,
    )
    block_signoff_rejection_reason = fields.Text(
        string="Sign-off Rejection Reason", readonly=True, copy=False,
    )
    block_kind = fields.Selection(
        selection=[
            ("credibility", "Credibility"),
            ("competence", "Competence"),
        ],
        string="Block Kind", copy=False,
        help="Required at co-sign time. Structurally selects the rejection "
             "letter: Credibility → the neutral \"unable to reconcile "
             "statements\" template; Competence → the experience-fit "
             "template (zero credibility vocabulary).",
    )
    clarifying_questions_markdown = fields.Text(
        string="Clarifying Questions (Markdown)", copy=False,
        help="Latest candidate-facing clarifying-question set generated "
             "for this HOLD screening — read by the HOLD verification "
             "email and the evidence wizard. Prior sets stay auditable on "
             "the clarification rows.",
    )
    clarifying_questions_html = fields.Html(
        string="Clarifying Questions",
        compute="_compute_clarifying_questions_html", sanitize=False,
    )
    clarification_ids = fields.One2many(
        "iris.clarification", "screening_id",
        string="Clarifying Question Runs",
    )

    # ------------------------------------------------------------------
    # CRUD / display
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("attempt") and vals.get("candidate_id"):
                vals["attempt"] = self.search_count(
                    [("candidate_id", "=", vals["candidate_id"])]
                ) + 1
            # The triggering actions (action_screen / action_rescreen) pass
            # requested_by_id explicitly; this default covers direct
            # creates so the "first screener" is never empty.
            vals.setdefault("requested_by_id", self.env.user.id)
        return super().create(vals_list)

    @api.depends("candidate_id.name", "attempt")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _(
                "%(candidate)s — Screening #%(attempt)s",
                candidate=rec.candidate_id.name or "?",
                attempt=rec.attempt or rec.id,
            )

    @api.depends("markdown_record")
    def _compute_markdown_html(self):
        for rec in self:
            rec.markdown_html = rec._markdown_to_html(rec.markdown_record)

    @api.depends("clarifying_questions_markdown")
    def _compute_clarifying_questions_html(self):
        for rec in self:
            rec.clarifying_questions_html = rec._markdown_to_html(
                rec.clarifying_questions_markdown
            )

    # ------------------------------------------------------------------
    # LLM template methods
    # ------------------------------------------------------------------
    def _llm_build_messages(self):
        """Build the SCREENING.md system prompt + INPUTS user block.

        The user message is assembled by the shared
        :func:`prompt_sanitizer.build_screening_inputs` builder (also used
        by ``iris.calibration.task``), which enforces the trust rules:

        * trusted/unfenced: the INPUTS header (TARGET ROLE / TODAY'S DATE),
          the role profile's competence guidance, and the central versioned
          tech-date table (``iris.tech.date.reference``);
        * fenced as untrusted data: the resume, the legacy free-text
          tech-date field (fallback when the central table is empty — else
          "none provided"), the approved-JD context (when the candidate is
          linked to an approved ``iris.job.description``), and — on
          re-screens — the recorded verification evidence + the prior HOLD
          record.

        The system prompt resolves role-first
        (``get_prompt(..., role=candidate.role_id)``).
        """
        self.ensure_one()
        candidate = self.candidate_id
        role = candidate.role_id
        system_prompt = prompt_loader.get_prompt(self.env, "screening", role=role)
        today = fields.Date.context_today(self)
        tech_table_md = self.env["iris.tech.date.reference"].get_reference_markdown()

        verification_evidence = None
        prior_hold_record = None
        if self.is_rescreen and self.parent_screening_id:
            parent = self.parent_screening_id
            verification_evidence = parent.verification_evidence or ""
            prior_hold_record = parent.markdown_record or ""

        user_text = prompt_sanitizer.build_screening_inputs(
            target_role=candidate.target_role,
            today_iso=today.isoformat(),
            tech_table_md=tech_table_md,
            resume_text=candidate.resume_text,
            role_guidance=role.competence_guidance or None,
            legacy_tech_dates=candidate.tech_date_reference,
            jd_context=candidate.jd_id.final_jd or None,
            verification_evidence=verification_evidence,
            prior_hold_record=prior_hold_record,
        )
        return system_prompt, user_text

    def _llm_on_success(self, content, meta):
        """Persist the screening record, parse the verdict, drive the state."""
        self.ensure_one()
        candidate = self.candidate_id
        now = fields.Datetime.now()
        self.sudo().write({
            "markdown_record": content,
            "screened_at": now,
        })

        date_str = fields.Date.context_today(self).isoformat()
        filename = f"screening-{self._candidate_lastname()}-{date_str}.md"
        attachment = self._make_md_attachment(
            filename, content, self._name, self.id,
        )
        self.sudo().write({"attachment_id": attachment.id})

        verdict = verdict_parser.parse_screening_verdict(content)
        if verdict is None:
            self.sudo().write({"llm_status": "needs_review"})
            candidate.sudo().write({"state": "needs_review"})
            candidate.message_post(body=_(
                "Screening #%(attempt)s completed but the verdict could not "
                "be parsed unambiguously — manager review required.",
                attempt=self.attempt,
            ))
            return

        self._apply_verdict(verdict)

    def _llm_on_failure(self, msg):
        """Revert the candidate deterministically: re-screen → hold, else draft."""
        self.ensure_one()
        candidate = self.candidate_id
        revert_state = "hold" if self.is_rescreen else "draft"
        candidate.sudo().write({"state": revert_state})
        candidate.message_post(body=_(
            "Screening #%(attempt)s failed (%(error)s). Candidate reverted "
            "to %(state)s.",
            attempt=self.attempt, error=msg, state=revert_state,
        ))

    # ------------------------------------------------------------------
    # Verdict application (shared: LLM parse path + manual manager path)
    # ------------------------------------------------------------------
    def _apply_verdict(self, verdict, manual=False):
        """Write ``verdict`` on this screening and apply candidate side effects.

        BLOCK is intercepted by the dual sign-off workflow (P0-1): the
        screening keeps the verdict immediately, ``block_signoff_state``
        goes ``pending`` with the proposer recorded (manual → the manager
        acting now; LLM → the user who requested the run), and the
        candidate moves to ``pending_block`` — never directly to
        ``blocked``. The daily auto-block cron is structurally exempt: its
        domain is ``state='hold'`` and it writes ``state`` directly.

        :param verdict: ``"ship"`` / ``"hold"`` / ``"block"``.
        :param manual: True when set by a manager from Needs Review.
        """
        self.ensure_one()
        if verdict not in _VERDICT_LABELS:
            raise UserError(_("Invalid verdict: %s") % verdict)
        candidate = self.candidate_id
        vals = {"verdict": verdict, "verdict_manual": manual}
        if not self.screened_at:
            vals["screened_at"] = fields.Datetime.now()

        if verdict == "ship":
            candidate.sudo().write({"state": "shipped", "hold_deadline": False})
        elif verdict == "block":
            proposer = (
                self.env.user if manual
                else (self.requested_by_id or self.env.user)
            )
            vals.update({
                "block_signoff_state": "pending",
                "block_proposed_by_id": proposer.id,
                "block_proposed_at": fields.Datetime.now(),
            })
            # hold_deadline deliberately NOT cleared: only the confirmed
            # co-sign (or a SHIP) ends the hold episode, preserving the
            # never-extended invariant across reject→HOLD cycles.
            candidate.sudo().write({"state": "pending_block"})
        else:  # hold
            candidate_vals = {"state": "hold"}
            if not candidate.hold_deadline:
                # First HOLD of the episode: start the business-day clock.
                # A re-HOLD keeps the ORIGINAL deadline (never extended).
                ICP = self.env["ir.config_parameter"].sudo()
                try:
                    hold_days = int(ICP.get_param("iris.hold_business_days", "5"))
                except (TypeError, ValueError):
                    hold_days = 5
                candidate_vals["hold_deadline"] = business_days.add_business_days(
                    fields.Date.context_today(self), hold_days,
                )
            candidate.sudo().write(candidate_vals)
            vals["hold_deadline"] = candidate.hold_deadline

        self.sudo().write(vals)
        candidate.message_post(body=_(
            "Screening #%(attempt)s verdict: %(verdict)s%(manual)s",
            attempt=self.attempt,
            verdict=_VERDICT_LABELS[verdict],
            manual=_(" (manual)") if manual else "",
        ))
        if verdict == "block":
            candidate.message_post(body=_(
                "BLOCK proposed by %(proposer)s — a second screener's "
                "sign-off is required before the candidate is blocked and "
                "before any communication is sent.",
                proposer=self.block_proposed_by_id.name or _("unknown"),
            ))
            candidate._schedule_block_signoff_activity()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_retry_llm(self):
        """Re-enqueue a failed screening (guarded). The retrier re-owns
        the run: a BLOCK proposed by this attempt chains to them."""
        self.ensure_one()
        if self.llm_status != "failed":
            raise UserError(_("Only failed screenings can be retried."))
        self.sudo().write({"requested_by_id": self.env.user.id})
        self.candidate_id.sudo().write({"state": "screening"})
        self._llm_enqueue()
        return True

    def action_generate_clarifying_questions(self):
        """Generate candidate-facing clarifying questions for this HOLD.

        One-shot LLM run (NOT a chat loop): one neutral, written question
        per open verification item in the HR-memo checklist, produced as an
        ``iris.clarification`` sub-artifact (regenerations create a new
        row; the latest set is denormalized onto
        ``clarifying_questions_markdown``).

        Guards: this screening's verdict is HOLD, the candidate is still on
        Hold, and the screening record is non-empty.
        """
        self.ensure_one()
        if self.verdict != "hold":
            raise UserError(_(
                "Clarifying questions can only be generated from a HOLD "
                "screening (this one is %s)."
            ) % (self.verdict or _("unresolved")))
        if self.candidate_id.state != "hold":
            raise UserError(_(
                "Clarifying questions are only available while the "
                "candidate is on Hold (current: %s)."
            ) % self.candidate_id.state)
        if not (self.markdown_record or "").strip():
            raise UserError(_(
                "This screening has no record to derive clarifying "
                "questions from."
            ))
        clarification = self.env["iris.clarification"].create({
            "screening_id": self.id,
        })
        clarification._llm_enqueue()
        return clarification

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _candidate_lastname(self):
        """Lowercased last whitespace token of the candidate name."""
        self.ensure_one()
        name = (self.candidate_id.name or "candidate").strip()
        return (name.split()[-1] if name.split() else "candidate").lower()
