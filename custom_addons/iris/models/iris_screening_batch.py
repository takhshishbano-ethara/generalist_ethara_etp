"""Iris screening batch — N individual screenings + 1 LLM consistency pass.

A batch groups ≥2 draft candidates sharing ONE role. ``action_screen_batch``
loops the existing ``candidate.action_screen()`` pipeline (full per-candidate
audit, untouched), then — once every member settles — the batch's OWN LLM
job (this model inherits ``iris.llm.job.mixin``) runs the cross-candidate
consistency review and produces an ADVISORY report. Advisory revisions are
never auto-applied: they become activities + chatter on the affected
candidates, and a manager-triggered re-screen applies them through the
normal evidence-gated path.

Completion detection is one idempotent choke point,
:meth:`_check_members_settled`, fed by (a) the ``iris.candidate.write()``
state hook and (b) the LLM queue cron's tail safety sweep. The single
state-guarded flip ``screening → consistency`` is the double-enqueue guard.

HOLD policy: HOLD and pending_block members COUNT as settled — the report
is a point-in-time artifact (like the reference team's dated report); the
manager "Re-run Consistency" button regenerates it after late re-screens,
keeping the prior report as an attachment revision.
"""

import base64
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import credential_manager
from ..services import prompt_loader, prompt_sanitizer, verdict_parser

_logger = logging.getLogger(__name__)

#: Candidate states in which a batch member counts as SETTLED for the
#: consistency pass. THE contract with the governance slice:
#: ``pending_block`` (verdict known, second sign-off pending) settles;
#: ``needs_review`` / ``screening`` / ``draft`` (failed-revert) block.
#: Imported by iris_candidate:
#: ``from .iris_screening_batch import BATCH_SETTLED_STATES``
BATCH_SETTLED_STATES = ("shipped", "blocked", "hold", "pending_block")

#: Default member cap (overridable via ICP ``iris.batch_max_members``).
_DEFAULT_MAX_MEMBERS = 10

#: Verdict labels used in the consistency-pass member delimiters, derived
#: from the candidate's settled STATE (not the raw screening verdict, so an
#: auto-blocked expired HOLD reads as BLOCK and a pending sign-off is
#: annotated per the BATCH_CONSISTENCY prompt contract).
_STATE_VERDICT_LABELS = {
    "shipped": "✅ SHIP",
    "hold": "⏸ HOLD",
    "blocked": "🚫 BLOCK",
    "pending_block": "🚫 BLOCK (pending second sign-off)",
}


class IrisScreeningBatch(models.Model):
    _name = "iris.screening.batch"
    _description = "Iris Screening Batch"
    _inherit = ["iris.llm.job.mixin", "mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    name = fields.Char(
        string="Batch Reference",
        default=lambda self: self.env["ir.sequence"].next_by_code(
            "iris.screening.batch"
        ),
        required=True,
        readonly=True,
        copy=False,
        index=True,
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("screening", "Screening"),
            ("consistency", "Consistency Review"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        string="State",
        default="draft",
        tracking=True,
        copy=False,
        index=True,
    )
    role_id = fields.Many2one(
        "iris.role.profile", string="Role",
        required=True, ondelete="restrict", tracking=True,
        domain=[("is_legacy", "=", False)],
        help="ONE role per batch — the consistency pass needs a single rule "
             "frame. Locked once the batch leaves Draft.",
    )
    candidate_ids = fields.One2many(
        "iris.candidate", "batch_id", string="Members",
    )
    screening_ids = fields.One2many(
        "iris.screening", "batch_id", string="Member Screenings",
    )
    user_id = fields.Many2one(
        "res.users", string="Batch Owner",
        default=lambda self: self.env.user, tracking=True,
        help="Assignee of the consistency advisory activities.",
    )
    auto_run_consistency = fields.Boolean(
        string="Auto-run Consistency Review", default=True,
        help="When all members settle, enqueue the consistency review "
             "automatically. Otherwise an activity asks the batch owner to "
             "trigger it manually.",
    )
    batch_report_markdown = fields.Text(
        string="Consistency Report (Markdown)", copy=False,
    )
    batch_report_html = fields.Html(
        string="Consistency Report", compute="_compute_batch_report_html",
        sanitize=False,
    )
    report_attachment_id = fields.Many2one(
        "ir.attachment", string="Report Attachment", copy=False,
        help="batch-report-{name}-{date}.md — re-runs APPEND revisions.",
    )
    consistency_findings_json = fields.Text(
        string="Consistency Findings (JSON)", copy=False,
        help="Parsed Machine Summary (schema iris.batch_consistency.v1). "
             "Empty when the machine summary could not be parsed — the "
             "batch still completes (fails OPEN).",
    )
    inconsistency_count = fields.Integer(
        string="Inconsistencies", copy=False,
    )
    revision_advisory_count = fields.Integer(
        string="Advisory Revisions", copy=False,
    )
    member_count = fields.Integer(
        string="Members", compute="_compute_member_counts",
    )
    settled_count = fields.Integer(
        string="Settled", compute="_compute_member_counts",
    )
    blocking_candidate_ids = fields.Many2many(
        "iris.candidate", string="Blocking Members",
        compute="_compute_blocking_candidate_ids",
        help="Members not yet settled (screening / needs review / "
             "failed-revert) — these hold the consistency pass back.",
    )
    total_cost_usd = fields.Float(
        string="Total Cost (USD)", digits=(12, 6),
        compute="_compute_total_cost_usd", store=True,
        help="Σ member screening costs + the consistency pass cost.",
    )
    notes = fields.Text(string="Notes")

    # ------------------------------------------------------------------
    # Computes / constraints
    # ------------------------------------------------------------------
    @api.depends("batch_report_markdown")
    def _compute_batch_report_html(self):
        for rec in self:
            rec.batch_report_html = rec._markdown_to_html(rec.batch_report_markdown)

    @api.depends("candidate_ids", "candidate_ids.state")
    def _compute_member_counts(self):
        for rec in self:
            rec.member_count = len(rec.candidate_ids)
            rec.settled_count = len(rec.candidate_ids.filtered(
                lambda c: c.state in BATCH_SETTLED_STATES
            ))

    @api.depends("candidate_ids", "candidate_ids.state")
    def _compute_blocking_candidate_ids(self):
        for rec in self:
            rec.blocking_candidate_ids = rec.candidate_ids.filtered(
                lambda c: c.state not in BATCH_SETTLED_STATES
            )

    @api.depends("screening_ids.llm_cost_usd", "llm_cost_usd")
    def _compute_total_cost_usd(self):
        for rec in self:
            rec.total_cost_usd = (
                sum(rec.screening_ids.mapped("llm_cost_usd")) + (rec.llm_cost_usd or 0.0)
            )

    @api.constrains("candidate_ids", "role_id")
    def _check_member_roles(self):
        for rec in self:
            mismatched = rec.candidate_ids.filtered(
                lambda c: c.role_id and c.role_id != rec.role_id
            )
            if mismatched:
                raise ValidationError(_(
                    "All batch members must share the batch role (%(role)s). "
                    "Mismatched: %(names)s",
                    role=rec.role_id.name,
                    names=", ".join(mismatched.mapped("name")),
                ))

    # ------------------------------------------------------------------
    # CRUD guards
    # ------------------------------------------------------------------
    def write(self, vals):
        if "role_id" in vals:
            for rec in self:
                if rec.state != "draft" and vals["role_id"] != rec.role_id.id:
                    raise UserError(_(
                        "The batch role is locked once screening has "
                        "started (batch %s)."
                    ) % rec.name)
        return super().write(vals)

    def unlink(self):
        for rec in self:
            if rec.state in ("screening", "consistency"):
                raise UserError(_(
                    "Batch %s cannot be deleted while screening or the "
                    "consistency review is in flight. Cancel it first."
                ) % rec.name)
        self.report_attachment_id.sudo().unlink()
        return super().unlink()

    # ------------------------------------------------------------------
    # Kickoff
    # ------------------------------------------------------------------
    def action_screen_batch(self):
        """Screen every member through the existing candidate pipeline."""
        self.ensure_one()
        if self.state != "draft":
            raise UserError(_(
                "Batch screening can only start from Draft (current: %s)."
            ) % self.state)
        members = self.candidate_ids
        if len(members) < 2:
            raise UserError(_(
                "A batch needs at least 2 members (a consistency review of "
                "one candidate is meaningless)."
            ))
        ICP = self.env["ir.config_parameter"].sudo()
        try:
            max_members = int(
                ICP.get_param("iris.batch_max_members", str(_DEFAULT_MAX_MEMBERS))
            )
        except (TypeError, ValueError):
            max_members = _DEFAULT_MAX_MEMBERS
        if len(members) > max_members:
            raise UserError(_(
                "A batch is capped at %(cap)s members "
                "(iris.batch_max_members); this one has %(count)s.",
                cap=max_members, count=len(members),
            ))
        not_draft = members.filtered(lambda c: c.state != "draft")
        if not_draft:
            raise UserError(_(
                "All members must be in Draft. Not draft: %s"
            ) % ", ".join(not_draft.mapped("name")))
        no_resume = members.filtered(lambda c: not (c.resume_text or "").strip())
        if no_resume:
            raise UserError(_(
                "All members need an uploaded resume. Missing: %s"
            ) % ", ".join(no_resume.mapped("name")))
        role_mismatch = members.filtered(lambda c: c.role_id != self.role_id)
        if role_mismatch:
            raise UserError(_(
                "All members must target the batch role (%(role)s). "
                "Mismatched: %(names)s",
                role=self.role_id.name,
                names=", ".join(role_mismatch.mapped("name")),
            ))
        # ONE upfront key check — fail fast before any member is touched.
        if not credential_manager.get_openrouter_api_key(self.env):
            raise UserError(_(
                "OpenRouter API key not configured. Set it in Settings → Iris."
            ))

        # Flip BEFORE looping so the candidate write-hook sees
        # batch.state == 'screening' from the first member onwards.
        self.write({"state": "screening"})
        self.message_post(body=_(
            "Batch screening started for %s member(s)."
        ) % len(members))
        for candidate in members:
            candidate.action_screen()
        return True

    # ------------------------------------------------------------------
    # Completion detection (idempotent choke point)
    # ------------------------------------------------------------------
    def _check_members_settled(self):
        """Flip ``screening → consistency`` once every member settles.

        Safe to call repeatedly on any recordset (both feeders do):

        * the ``iris.candidate.write()`` hook on candidate state changes,
        * the LLM queue cron's tail safety sweep over screening batches.

        Idempotency: only batches still in ``screening`` are considered
        (state-guard re-read), and the single write flipping the state is
        the double-enqueue guard. When ``auto_run_consistency`` is False
        (or no API key is configured) the batch STAYS in ``screening`` and
        a deduplicated activity asks the owner to trigger the review.
        """
        for batch in self:
            if batch.state != "screening":
                continue
            members = batch.candidate_ids
            if len(members) < 2:
                # Member deletion dropped the batch below viability.
                batch.write({"state": "cancelled"})
                batch.message_post(body=_(
                    "Batch auto-cancelled: fewer than 2 members remain."
                ))
                continue
            if any(m.state not in BATCH_SETTLED_STATES for m in members):
                continue
            if not batch.auto_run_consistency:
                batch._notify_consistency_ready(_(
                    "All %s members have settled. Auto-run is disabled — "
                    "use the Run Consistency Review button."
                ) % len(members))
                continue
            if not credential_manager.get_openrouter_api_key(batch.env):
                batch._notify_consistency_ready(_(
                    "All %s members have settled but no OpenRouter API key "
                    "is configured. Set it in Settings → Iris, then use the "
                    "Run Consistency Review button."
                ) % len(members))
                continue
            batch._start_consistency()
        return True

    def _notify_consistency_ready(self, body):
        """Chatter + ONE activity asking the owner to run the review.

        Deduplicated so the cron safety sweep never spams: a second call
        with an identical open activity is a no-op.
        """
        self.ensure_one()
        summary = _("Run consistency review — %s") % self.name
        existing = self.env["mail.activity"].sudo().search([
            ("res_model", "=", self._name),
            ("res_id", "=", self.id),
            ("summary", "=", summary),
        ], limit=1)
        if existing:
            return
        self.message_post(body=body)
        self.activity_schedule(
            "mail.mail_activity_data_todo",
            user_id=(self.user_id or self.env.user).id,
            summary=summary,
            note=body,
        )

    def _start_consistency(self):
        """Single state-guarded flip to ``consistency`` + enqueue."""
        self.ensure_one()
        self.write({"state": "consistency"})
        self.message_post(body=_(
            "All members settled — consistency review queued."
        ))
        self.activity_unlink(["mail.mail_activity_data_todo"])
        self._llm_enqueue()
        return True

    # ------------------------------------------------------------------
    # Manual triggers
    # ------------------------------------------------------------------
    def action_run_consistency(self):
        """Manually trigger the consistency review (manual mode / no key)."""
        self.ensure_one()
        if self.state != "screening":
            raise UserError(_(
                "The consistency review can only be triggered while the "
                "batch is in Screening (current: %s)."
            ) % self.state)
        members = self.candidate_ids
        if len(members) < 2:
            raise UserError(_("A batch needs at least 2 members."))
        pending = members.filtered(lambda c: c.state not in BATCH_SETTLED_STATES)
        if pending:
            raise UserError(_(
                "Not all members have settled yet. Waiting on: %s"
            ) % ", ".join(pending.mapped("name")))
        return self._start_consistency()

    def action_rerun_consistency(self):
        """Manager re-run after late re-screens (Done batches only).

        The prior report is preserved as an attachment revision; its cost
        and model are noted in the chatter before the metadata is
        overwritten by the new run.
        """
        self.ensure_one()
        if not self.env.user.has_group("iris.group_iris_manager"):
            raise UserError(_(
                "Only Iris Managers can re-run a consistency review."
            ))
        if self.state != "done":
            raise UserError(_(
                "Re-run is only available on a completed batch (current: %s)."
            ) % self.state)
        self.message_post(body=_(
            "Re-running consistency review. Prior run: model %(model)s, "
            "cost $%(cost).4f — the prior report is preserved as a "
            "revision in the attachment.",
            model=self.llm_model_used or _("unknown"),
            cost=self.llm_cost_usd or 0.0,
        ))
        self.write({"state": "consistency"})
        self._llm_enqueue()
        return True

    def action_retry_llm(self):
        """Re-enqueue a failed consistency pass (guarded)."""
        self.ensure_one()
        if self.llm_status != "failed":
            raise UserError(_(
                "Only a failed consistency review can be retried."
            ))
        self.write({"state": "consistency"})
        self._llm_enqueue()
        return True

    def action_cancel(self):
        """Cancel the batch (draft/screening). Member screenings already in
        flight continue on the individual candidates."""
        for batch in self:
            if batch.state not in ("draft", "screening"):
                raise UserError(_(
                    "Batch %(name)s cannot be cancelled from %(state)s.",
                    name=batch.name, state=batch.state,
                ))
            batch.write({"state": "cancelled"})
            batch.activity_unlink(["mail.mail_activity_data_todo"])
            batch.message_post(body=_(
                "Batch cancelled. Member screenings already in flight "
                "continue on the individual candidates."
            ))
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _member_latest_screenings(self):
        """[(candidate, latest screening with a verdict)] per member.

        The screening recordset is empty for a member with no verdict-bearing
        screening (defensive — settled members always have one).
        """
        self.ensure_one()
        result = []
        for candidate in self.candidate_ids:
            screening = candidate.screening_ids.filtered(
                lambda s: s.verdict
            ).sorted("id")[-1:]
            result.append((candidate, screening))
        return result

    def _member_verdict_label(self, candidate):
        """Delimiter verdict label derived from the candidate's state."""
        return _STATE_VERDICT_LABELS.get(
            candidate.state, candidate.state or "?",
        )

    # ------------------------------------------------------------------
    # LLM template methods (the batch's own LLM job IS the consistency pass)
    # ------------------------------------------------------------------
    def _llm_build_messages(self):
        """BATCH_CONSISTENCY prompt + N delimited member records.

        Each member's screening record embeds candidate-supplied resume
        quotes, so every record is fenced as untrusted via the prompt
        sanitizer; only the INPUTS block and the delimiters are trusted
        scaffolding.
        """
        self.ensure_one()
        system_prompt = prompt_loader.get_prompt(
            self.env, "batch_consistency", role=self.role_id,
        )
        today = fields.Date.context_today(self)
        members = self._member_latest_screenings()
        total = len(members)

        lines = [
            "INPUTS:",
            "```",
            f"ROLE / LEVEL:        {self.role_id.name or ''}",
            f"TODAY'S DATE:        {today.isoformat()}",
            f"MEMBER COUNT:        {total}",
            "```",
            "",
        ]
        for index, (candidate, screening) in enumerate(members, start=1):
            header = (
                f"===== CANDIDATE {index}/{total}: "
                f"{candidate.reference or '?'} — {candidate.name} — "
                f"VERDICT: {self._member_verdict_label(candidate)} ====="
            )
            record = screening.markdown_record or _(
                "(no screening record available)"
            )
            lines += [
                header,
                prompt_sanitizer.fence_untrusted("CANDIDATE RECORD", record),
                f"===== END CANDIDATE {index}/{total} =====",
                "",
            ]
        return system_prompt, "\n".join(lines)

    def _llm_on_success(self, content, meta):
        """Store the report, attach it, parse the Machine Summary, raise
        advisory activities, and complete the batch.

        Parse failure FAILS OPEN: the advisory output never holds the batch
        hostage to JSON drift — the batch still reaches ``done`` with a
        warning in the chatter.
        """
        self.ensure_one()
        self.sudo().write({"batch_report_markdown": content})
        self._attach_report(content)

        parsed = verdict_parser.parse_batch_consistency(content)
        if parsed is None:
            self.sudo().write({
                "consistency_findings_json": False,
                "inconsistency_count": 0,
                "revision_advisory_count": 0,
                "state": "done",
            })
            self.message_post(body=_(
                "Consistency review complete, but the machine summary was "
                "unparseable — advisory activities were NOT created; read "
                "the report manually."
            ))
            return

        inconsistencies = parsed.get("inconsistencies") or []
        candidate_entries = parsed.get("candidates") or []
        revisions = [
            entry for entry in candidate_entries
            if entry.get("revision_recommended")
        ]
        self.sudo().write({
            "consistency_findings_json": json.dumps(
                parsed, ensure_ascii=False,
            ),
            "inconsistency_count": len(inconsistencies),
            "revision_advisory_count": len(revisions),
            "state": "done",
        })
        self._raise_advisory_activities(revisions, inconsistencies)
        self.message_post(body=_(
            "Consistency review complete: %(inconsistencies)s "
            "inconsistency finding(s), %(revisions)s advisory "
            "revision(s). Advisory only — verdicts change only through a "
            "human-triggered re-screen.",
            inconsistencies=len(inconsistencies),
            revisions=len(revisions),
        ))

    def _raise_advisory_activities(self, revisions, inconsistencies):
        """Activities + chatter on affected candidates. NEVER auto-applies."""
        self.ensure_one()
        by_reference = {
            candidate.reference: candidate for candidate in self.candidate_ids
        }
        affected = {}
        for entry in revisions:
            candidate = by_reference.get(entry.get("reference"))
            if candidate:
                affected.setdefault(candidate, []).append(_(
                    "Advisory revision: %(current)s → %(recommended)s",
                    current=(entry.get("current_verdict") or "?").upper(),
                    recommended=(entry.get("revision_recommended") or "?").upper(),
                ))
        for finding in inconsistencies:
            for reference in finding.get("should_fire_on") or []:
                candidate = by_reference.get(reference)
                if candidate:
                    affected.setdefault(candidate, []).append(_(
                        "Flag %(flag)s fired elsewhere in the batch and "
                        "should be evaluated here: %(evidence)s",
                        flag=finding.get("flag") or "?",
                        evidence=finding.get("evidence") or "",
                    ))
        assignee = (self.user_id or self.env.user).id
        for candidate, reasons in affected.items():
            note = "\n".join(f"- {reason}" for reason in reasons)
            candidate.activity_schedule(
                "mail.mail_activity_data_todo",
                user_id=assignee,
                summary=_("Batch consistency advisory — %s") % self.name,
                note=note,
            )
            candidate.message_post(body=_(
                "Batch consistency advisory from %(batch)s (see the batch "
                "report):\n%(note)s",
                batch=self.name, note=note,
            ))

    def _attach_report(self, content):
        """Create or append-revise ``batch-report-{name}-{date}.md``."""
        self.ensure_one()
        attachment = self.report_attachment_id
        if attachment:
            try:
                existing = base64.b64decode(attachment.datas or b"").decode(
                    "utf-8", errors="replace",
                )
            except Exception:  # noqa: BLE001 — never lose the new report
                existing = ""
            revised = (
                f"{existing}\n\n---\n\n"
                f"# Revision — {fields.Datetime.now().isoformat()}\n\n"
                f"{content}"
            )
            attachment.sudo().write({
                "datas": base64.b64encode(revised.encode("utf-8")),
            })
            return
        date_str = fields.Date.context_today(self).isoformat()
        filename = f"batch-report-{self.name}-{date_str}.md"
        attachment = self._make_md_attachment(
            filename, content, self._name, self.id,
        )
        self.sudo().write({"report_attachment_id": attachment.id})

    def _llm_on_failure(self, msg):
        """Consistency pass failed: batch → failed; members untouched."""
        self.ensure_one()
        self.sudo().write({"state": "failed"})
        self.message_post(body=_(
            "Consistency review failed (%s). Member verdicts are "
            "unaffected — use Retry to re-run the review."
        ) % msg)
