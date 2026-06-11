"""Iris candidate — the hiring-pipeline anchor record.

A standalone model (deliberately NOT ``hr.applicant``): one row per
candidate, carrying the resume (binary + extracted text + private S3 copy)
and the pipeline ``state`` driven exclusively by action methods, LLM
callbacks and crons.

State machine::

    draft → screening → shipped | hold | pending_block | needs_review
    screening: LLM block → pending_block (dual sign-off, v1.1)
    pending_block → (co-sign by a DIFFERENT screener) → blocked
    pending_block → (sign-off rejected + reason) → needs_review
    shipped → interview_ready → interviewed → scored → hired | rejected
    hold → (evidence + re-screen) → screening → ...
    hold → (daily cron past hold_deadline) → blocked   (sign-off EXEMPT)

``hold_deadline`` is set on the FIRST hold, never extended by a re-HOLD,
kept while ``pending_block`` (the never-extended invariant survives
reject→HOLD cycles), and cleared on ship/confirmed block (end of the
hold episode).
"""

import base64
import logging
import uuid

from werkzeug.utils import secure_filename

from odoo import _, api, fields, models, modules, tools
from odoo.exceptions import UserError

from ..services import duplicate_detector, pdf_extractor
from .iris_screening_batch import BATCH_SETTLED_STATES

_logger = logging.getLogger(__name__)

#: Models swept by the LLM queue cron, in declaration order.
_LLM_QUEUE_MODELS = (
    "iris.screening",
    "iris.interview",
    "iris.scorecard",
    "iris.screening.batch",
    "iris.calibration.task",
    "iris.jd.artifact",
    "iris.assessment",
    "iris.clarification",
)

#: Valid kinds for a co-signed BLOCK (drives the rejection-mail template).
_BLOCK_KINDS = ("credibility", "competence")

#: Minutes after which a ``running`` LLM job is considered stuck and reaped.
_LLM_RUNNING_REAP_MINUTES = 30


class IrisCandidate(models.Model):
    _name = "iris.candidate"
    _description = "Iris Candidate"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    _unique_reference = models.Constraint(
        "UNIQUE(reference)",
        message="Candidate reference must be unique.",
    )

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    name = fields.Char(string="Candidate Name", required=True, tracking=True)
    reference = fields.Char(
        string="Reference",
        default=lambda self: self.env["ir.sequence"].next_by_code("iris.candidate"),
        readonly=True,
        copy=False,
        index=True,
    )
    email = fields.Char(string="Email")
    phone = fields.Char(string="Phone")
    role_id = fields.Many2one(
        "iris.role.profile", string="Role",
        required=True, ondelete="restrict", tracking=True,
        domain=[("is_legacy", "=", False)],
        default=lambda self: self.env.ref(
            "iris.role_head_of_engineering", raise_if_not_found=False,
        ),
        help="The role profile this candidate targets. Drives the role-aware "
             "prompts and the tech-date snapshot at creation.",
    )
    target_role = fields.Char(
        string="Target Role / Level",
        related="role_id.name", store=True, readonly=True,
        help="Stored related to the role profile name — kept under the v1.0 "
             "field name so serializers, prompts and domains read unchanged.",
    )
    jd_id = fields.Many2one(
        "iris.job.description", string="Approved Job Description",
        domain=[("state", "=", "approved")], ondelete="set null",
        help="Optional: links an APPROVED job description; its final text is "
             "appended to the screening prompt as ROLE CONTEXT.",
    )
    batch_id = fields.Many2one(
        "iris.screening.batch", string="Screening Batch",
        ondelete="set null", copy=False, index=True,
        help="The screening batch this candidate belongs to (optional — the "
             "single-candidate flow stays first-class).",
    )
    tech_date_reference = fields.Text(
        string="Tech Date Reference",
        help="Optional table of release/GA dates for claimed technologies; "
             "passed to the screening prompt as TECH DATE REFERENCE. "
             "Deprecated in v1.1 in favour of the central versioned table; "
             "snapshotted from the role profile at creation when empty.",
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("screening", "Screening"),
            ("hold", "Hold"),
            ("blocked", "Blocked"),
            ("pending_block", "Pending BLOCK Sign-off"),
            ("shipped", "Shipped"),
            ("interview_ready", "Interview Ready"),
            ("interviewed", "Interviewed"),
            ("scored", "Scored"),
            ("hired", "Hired"),
            ("rejected", "Rejected"),
            ("needs_review", "Needs Review"),
        ],
        string="State",
        default="draft",
        tracking=True,
        copy=False,
        index=True,
    )
    resume_file = fields.Binary(string="Resume (PDF)", attachment=True, copy=False)
    resume_filename = fields.Char(string="Resume Filename", copy=False)
    resume_text = fields.Text(
        string="Resume Text", readonly=True, copy=False,
        help="Plain text extracted from the PDF via PyMuPDF; this is what "
             "the LLM actually reads.",
    )
    resume_s3_key = fields.Char(string="Resume S3 Key", readonly=True, copy=False)
    resume_uploaded_at = fields.Datetime(
        string="Resume Uploaded At", readonly=True, copy=False,
    )
    resume_sha256 = fields.Char(
        string="Resume Fingerprint (SHA-256)", readonly=True, index=True,
        copy=False,
        help="SHA-256 of the normalized resume text — the indexed "
             "exact-duplicate fingerprint.",
    )
    duplicate_candidate_ids = fields.Many2many(
        "iris.candidate",
        relation="iris_candidate_duplicate_rel",
        column1="candidate_id",
        column2="duplicate_id",
        string="Possible Duplicates",
        copy=False,
        help="Candidates whose resume is an exact or near duplicate of this "
             "one. ADVISORY ONLY — never blocks, never touches state, never "
             "enters the LLM prompt.",
    )
    duplicate_warning = fields.Text(
        string="Duplicate Warning", readonly=True, copy=False,
    )
    hold_deadline = fields.Date(
        string="Hold Deadline", copy=False, tracking=True,
        help="Set on the FIRST hold of the current episode; never extended "
             "by a re-HOLD; cleared on ship/block. Holds past this date are "
             "auto-blocked by the daily cron.",
    )
    active = fields.Boolean(default=True)

    screening_ids = fields.One2many(
        "iris.screening", "candidate_id", string="Screenings",
    )
    interview_ids = fields.One2many(
        "iris.interview", "candidate_id", string="Interviews",
    )
    scorecard_ids = fields.One2many(
        "iris.scorecard", "candidate_id", string="Scorecards",
    )
    assessment_ids = fields.One2many(
        "iris.assessment", "candidate_id", string="Assessments",
    )

    current_screening_id = fields.Many2one(
        "iris.screening", string="Current Screening",
        compute="_compute_current_artifacts",
        help="Latest screening whose LLM run completed (done).",
    )
    current_verdict = fields.Selection(
        selection=[("ship", "Ship"), ("hold", "Hold"), ("block", "Block")],
        string="Current Verdict",
        compute="_compute_current_artifacts",
    )
    current_interview_id = fields.Many2one(
        "iris.interview", string="Current Interview",
        compute="_compute_current_artifacts",
    )
    current_scorecard_id = fields.Many2one(
        "iris.scorecard", string="Current Scorecard",
        compute="_compute_current_artifacts",
    )
    final_recommendation = fields.Selection(
        selection=[
            ("strong_hire", "Strong Hire"),
            ("hire", "Hire"),
            ("no_hire", "No Hire"),
            ("strong_no_hire", "Strong No Hire"),
        ],
        string="Final Recommendation",
        compute="_compute_current_artifacts",
    )
    block_proposed_by_id = fields.Many2one(
        "res.users", string="BLOCK Proposed By",
        related="current_screening_id.block_proposed_by_id",
        help="Who proposed the pending BLOCK (form banner).",
    )
    screening_count = fields.Integer(compute="_compute_counts")
    interview_count = fields.Integer(compute="_compute_counts")
    scorecard_count = fields.Integer(compute="_compute_counts")
    assessment_count = fields.Integer(compute="_compute_counts")
    duplicate_count = fields.Integer(compute="_compute_counts")

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends(
        "screening_ids.llm_status", "screening_ids.verdict",
        "interview_ids", "scorecard_ids.recommendation",
        "scorecard_ids.llm_status",
    )
    def _compute_current_artifacts(self):
        for rec in self:
            done_screenings = rec.screening_ids.filtered(
                lambda s: s.llm_status == "done"
            ).sorted("id")
            rec.current_screening_id = done_screenings[-1:].id or False
            rec.current_verdict = (
                done_screenings[-1:].verdict if done_screenings else False
            )
            interviews = rec.interview_ids.sorted("id")
            rec.current_interview_id = interviews[-1:].id or False
            scorecards = rec.scorecard_ids.sorted("id")
            rec.current_scorecard_id = scorecards[-1:].id or False
            rec.final_recommendation = (
                scorecards[-1:].recommendation if scorecards else False
            )

    @api.depends(
        "screening_ids", "interview_ids", "scorecard_ids",
        "assessment_ids", "duplicate_candidate_ids",
    )
    def _compute_counts(self):
        for rec in self:
            rec.screening_count = len(rec.screening_ids)
            rec.interview_count = len(rec.interview_ids)
            rec.scorecard_count = len(rec.scorecard_ids)
            rec.assessment_count = len(rec.assessment_ids)
            rec.duplicate_count = len(rec.duplicate_candidate_ids)

    # ------------------------------------------------------------------
    # CRUD + resume processing
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec, vals in zip(records, vals_list):
            # Snapshot semantics: the role's starter tech-date table is
            # copied ONCE at creation (later role-profile edits never
            # rewrite history on existing candidates).
            if (
                not (rec.tech_date_reference or "").strip()
                and rec.role_id.default_tech_date_reference
            ):
                rec.tech_date_reference = rec.role_id.default_tech_date_reference
            if vals.get("resume_file"):
                rec._process_resume(vals["resume_file"], vals.get("resume_filename"))
        return records

    def write(self, vals):
        res = super().write(vals)
        if "resume_file" in vals:
            if vals["resume_file"]:
                for rec in self:
                    rec._process_resume(
                        vals["resume_file"],
                        vals.get("resume_filename") or rec.resume_filename,
                    )
            else:
                super().write({
                    "resume_text": False,
                    "resume_filename": False,
                    "resume_s3_key": False,
                    "resume_uploaded_at": False,
                    "resume_sha256": False,
                    "duplicate_candidate_ids": [(5, 0, 0)],
                    "duplicate_warning": False,
                })
        # Batch completion feeder: a member just settled — give its batch a
        # chance to flip screening → consistency (idempotent choke point;
        # the queue cron's tail sweep is the safety net).
        if "state" in vals and vals.get("state") in BATCH_SETTLED_STATES:
            batches = self.batch_id.filtered(lambda b: b.state == "screening")
            if batches:
                batches._check_members_settled()
        return res

    def _process_resume(self, resume_b64, filename):
        """Extract text from a freshly written resume and mirror it to S3.

        Text extraction failure is blocking (raises :class:`UserError` —
        the resume is unusable for screening). S3 mirroring is best-effort:
        a missing connector or boto3 failure posts a chatter note and the
        pipeline continues with the local attachment copy.

        :param resume_b64: base64-encoded PDF payload (as written to the
            Binary field).
        :param filename: original filename (used for logs + the S3 key).
        """
        self.ensure_one()
        if isinstance(resume_b64, str):
            resume_b64 = resume_b64.encode("ascii")
        try:
            raw = base64.b64decode(resume_b64)
        except Exception as exc:
            raise UserError(_("Could not decode the uploaded resume: %s") % exc) from exc

        try:
            text = pdf_extractor.extract_text_from_pdf(raw, filename or "")
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        vals = {
            "resume_text": text,
            "resume_uploaded_at": fields.Datetime.now(),
        }

        try:
            connector = self._get_s3_connector()
            if connector:
                safe_name = secure_filename(filename or "resume.pdf") or "resume.pdf"
                object_key = f"iris/resumes/{self.id}/{uuid.uuid4().hex}_{safe_name}"
                self.env["iris.s3.storage"].put_object_bytes(
                    connector.id, object_key, raw, mimetype="application/pdf",
                )
                vals["resume_s3_key"] = object_key
            else:
                self.message_post(body=_(
                    "Resume kept locally only: no S3 connector is configured "
                    "(Settings → Iris)."
                ))
        except Exception as exc:  # noqa: BLE001 — S3 mirroring is best-effort
            _logger.warning(
                "Iris: S3 resume upload failed for candidate %s: %s", self.id, exc,
            )
            self.message_post(body=_(
                "Resume kept locally only: S3 upload failed (%s)."
            ) % exc)

        self.sudo().write(vals)

        # Cross-candidate duplicate detection (P2-10) — best-effort ONLY:
        # a detector crash must never break the resume upload.
        try:
            self._detect_duplicate_resumes()
        except Exception:  # noqa: BLE001 — advisory signal, never blocking
            _logger.warning(
                "Iris: duplicate-resume detection failed for candidate %s",
                self.id, exc_info=True,
            )

    def _detect_duplicate_resumes(self):
        """Advisory cross-candidate duplicate-resume scan.

        Stores the SHA-256 fingerprint of the normalized resume text, then
        looks for (1) exact matches via the indexed hash and (2) bounded
        difflib near-duplicates over the most recent ``iris.dup_scan_limit``
        candidates at ``iris.dup_similarity_threshold``. Hits are written to
        the M2M + warning banner and posted to the chatter on BOTH sides.

        Never blocks, never touches ``state``, never enters the LLM prompt
        (cross-candidate analysis is the batch consistency pass's job).
        """
        self.ensure_one()
        normalized = duplicate_detector.normalize_resume_text(self.resume_text)
        sha = duplicate_detector.sha256_of(normalized) if normalized else False
        self.sudo().write({"resume_sha256": sha})
        if not normalized:
            return

        ICP = self.env["ir.config_parameter"].sudo()
        try:
            threshold = float(ICP.get_param(
                "iris.dup_similarity_threshold",
                str(duplicate_detector.DEFAULT_SIMILARITY_THRESHOLD),
            ))
        except (TypeError, ValueError):
            threshold = duplicate_detector.DEFAULT_SIMILARITY_THRESHOLD
        try:
            scan_limit = int(ICP.get_param("iris.dup_scan_limit", "200"))
        except (TypeError, ValueError):
            scan_limit = 200

        Candidate = self.env["iris.candidate"].sudo().with_context(
            active_test=False,
        )
        exact = Candidate.search([
            ("resume_sha256", "=", sha),
            ("id", "!=", self.id),
        ])
        hits = [(candidate, 1.0) for candidate in exact]
        if scan_limit > 0:
            recent = Candidate.search(
                [("id", "!=", self.id), ("resume_text", "!=", False)],
                order="id desc",
                limit=scan_limit,
            )
            others = [
                (cand, duplicate_detector.normalize_resume_text(cand.resume_text))
                for cand in (recent - exact)
            ]
            hits += duplicate_detector.find_near_duplicates(
                normalized, others, threshold,
            )

        if not hits:
            self.sudo().write({
                "duplicate_candidate_ids": [(5, 0, 0)],
                "duplicate_warning": False,
            })
            return

        def _describe(other, ratio):
            label = (
                _("exact match") if ratio >= 0.9995
                else _("%s%% similar") % int(round(ratio * 100))
            )
            return f"{other.reference or other.id} — {other.name} ({label})"

        warning = _("Possible duplicate resume(s) detected: %s") % "; ".join(
            _describe(other, ratio) for other, ratio in hits
        )
        self.sudo().write({
            "duplicate_candidate_ids": [(6, 0, [other.id for other, _ratio in hits])],
            "duplicate_warning": warning,
        })
        self.message_post(body=warning)
        for other, ratio in hits:
            other_warning = _(
                "Possible duplicate resume(s) detected: %(ref)s — %(name)s "
                "(%(label)s)",
                ref=self.reference or self.id,
                name=self.name,
                label=(
                    _("exact match") if ratio >= 0.9995
                    else _("%s%% similar") % int(round(ratio * 100))
                ),
            )
            other.sudo().write({
                "duplicate_candidate_ids": [(4, self.id)],
                "duplicate_warning": (
                    f"{other.duplicate_warning}\n{other_warning}"
                    if other.duplicate_warning else other_warning
                ),
            })
            other.message_post(body=other_warning)

    def _get_s3_connector(self):
        """Return the configured ``s3.connector`` (ICP id, else the first)."""
        ICP = self.env["ir.config_parameter"].sudo()
        connector_id = ICP.get_param("iris.s3_connector_id", "")
        Connector = self.env["s3.connector"].sudo()
        if connector_id:
            try:
                connector = Connector.browse(int(connector_id))
                if connector.exists():
                    return connector
            except (TypeError, ValueError):
                pass
        return Connector.search([], limit=1)

    # ------------------------------------------------------------------
    # Pipeline actions (single code path for UI buttons + REST API)
    # ------------------------------------------------------------------
    def action_screen(self):
        """Trigger the initial LLM screening (draft → screening)."""
        self.ensure_one()
        if self.state != "draft":
            raise UserError(_(
                "Screening can only be started from Draft (current: %s)."
            ) % self.state)
        if not (self.resume_text or "").strip():
            raise UserError(_("Upload a PDF resume before screening."))
        screening = self.env["iris.screening"].create({
            "candidate_id": self.id,
            "requested_by_id": self.env.user.id,
        })
        self.write({"state": "screening"})
        screening._llm_enqueue()
        return screening

    def action_open_evidence_wizard(self):
        """Open the verification-evidence wizard for a HOLD candidate."""
        self.ensure_one()
        if self.state != "hold":
            raise UserError(_("Evidence can only be recorded on a candidate on Hold."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Record Verification Evidence"),
            "res_model": "iris.evidence.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_candidate_id": self.id},
        }

    def action_rescreen(self):
        """Re-screen a HOLD candidate after verification evidence is recorded."""
        self.ensure_one()
        if self.state != "hold":
            raise UserError(_(
                "Re-screening is only available for candidates on Hold."
            ))
        hold_screening = self._get_current_hold_screening()
        if not hold_screening:
            raise UserError(_("No HOLD screening found to re-screen against."))
        if not (hold_screening.verification_evidence or "").strip():
            raise UserError(_(
                "Record verification evidence before re-screening "
                "(use the Record Evidence button)."
            ))
        screening = self.env["iris.screening"].create({
            "candidate_id": self.id,
            "is_rescreen": True,
            "parent_screening_id": hold_screening.id,
            "rescreen_reason": "hold_evidence",
            "requested_by_id": self.env.user.id,
        })
        self.write({"state": "screening"})
        screening._llm_enqueue()
        return screening

    def action_rescreen_advisory(self):
        """Manager re-screen from a batch-consistency advisory (v1.1).

        The batch finding IS the recorded evidence — it is written onto the
        latest verdict-bearing screening, honoring the "no evidence, no
        re-screen" invariant. Available only once the batch's consistency
        report exists (batch ``done``) for members that settled as
        shipped/blocked.
        """
        self.ensure_one()
        if not self.env.user.has_group("iris.group_iris_manager"):
            raise UserError(_(
                "Only Iris Managers can re-screen from a batch advisory."
            ))
        if self.state not in ("shipped", "blocked"):
            raise UserError(_(
                "Advisory re-screens are only available for Shipped or "
                "Blocked candidates (current: %s)."
            ) % self.state)
        if not self.batch_id or self.batch_id.state != "done":
            raise UserError(_(
                "Advisory re-screens require a completed batch consistency "
                "review (batch report)."
            ))
        parent = self.screening_ids.filtered(
            lambda s: s.verdict
        ).sorted("id")[-1:]
        if not parent:
            raise UserError(_("No prior screening found to re-screen against."))
        if not (parent.verification_evidence or "").strip():
            parent.sudo().write({
                "verification_evidence": _(
                    "Batch consistency advisory from %s — see the batch "
                    "report for the finding and its evidence."
                ) % self.batch_id.name,
                "evidence_recorded_by": self.env.user.id,
                "evidence_recorded_at": fields.Datetime.now(),
            })
        screening = self.env["iris.screening"].create({
            "candidate_id": self.id,
            "is_rescreen": True,
            "parent_screening_id": parent.id,
            "rescreen_reason": "batch_consistency",
            "requested_by_id": self.env.user.id,
        })
        self.write({"state": "screening"})
        self.message_post(body=_(
            "Advisory re-screen triggered by %(user)s from batch %(batch)s.",
            user=self.env.user.name, batch=self.batch_id.name,
        ))
        screening._llm_enqueue()
        return screening

    def action_generate_guide(self):
        """Generate the interview guide for a SHIPPED candidate."""
        self.ensure_one()
        if self.state != "shipped":
            raise UserError(_(
                "Interview guides can only be generated for Shipped candidates."
            ))
        interview = self.env["iris.interview"].create({
            "candidate_id": self.id,
            "screening_id": self.current_screening_id.id or False,
        })
        interview._llm_enqueue()
        return interview

    def action_generate_scorecard(self):
        """Generate the scorecard from the current interview's notes."""
        self.ensure_one()
        if self.state != "interviewed":
            raise UserError(_(
                "Scorecards can only be generated once the interview notes "
                "are submitted."
            ))
        interview = self.current_interview_id
        if not interview:
            raise UserError(_("No interview found for this candidate."))
        return interview.action_generate_scorecard()

    def action_mark_hired(self):
        """Final decision: hired (from scored)."""
        self.ensure_one()
        if self.state != "scored":
            raise UserError(_("Only Scored candidates can be marked Hired."))
        self.write({"state": "hired"})
        self.message_post(body=_("Candidate marked as Hired."))
        return True

    def action_mark_rejected(self):
        """Final decision: rejected (from scored)."""
        self.ensure_one()
        if self.state != "scored":
            raise UserError(_("Only Scored candidates can be marked Rejected."))
        self.write({"state": "rejected"})
        self.message_post(body=_("Candidate marked as Rejected."))
        return True

    # ------------------------------------------------------------------
    # Manual verdict (manager-only, from needs_review)
    # ------------------------------------------------------------------
    def action_manual_verdict_ship(self):
        return self._apply_manual_verdict("ship")

    def action_manual_verdict_hold(self):
        return self._apply_manual_verdict("hold")

    def action_manual_verdict_block(self):
        return self._apply_manual_verdict("block")

    def _apply_manual_verdict(self, verdict):
        """Manager override when the LLM verdict could not be parsed."""
        self.ensure_one()
        if not self.env.user.has_group("iris.group_iris_manager"):
            raise UserError(_(
                "Only Iris Managers can set a manual verdict."
            ))
        if self.state != "needs_review":
            raise UserError(_(
                "Manual verdicts are only available from Needs Review."
            ))
        screening = self.screening_ids.filtered(
            lambda s: s.llm_status == "needs_review"
        ).sorted("id")[-1:]
        if not screening:
            raise UserError(_("No screening awaiting review was found."))
        screening.sudo().write({"llm_status": "done"})
        screening._apply_verdict(verdict, manual=True)
        self.message_post(body=_(
            "Manual verdict set by %(user)s: %(verdict)s",
            user=self.env.user.name, verdict=verdict.upper(),
        ))
        return True

    # ------------------------------------------------------------------
    # Dual sign-off on BLOCK (v1.1, P0-1)
    # ------------------------------------------------------------------
    def action_open_block_signoff_wizard(self):
        """Open the co-sign wizard for a pending BLOCK."""
        self.ensure_one()
        if self.state != "pending_block":
            raise UserError(_(
                "BLOCK sign-off is only available while the candidate is "
                "Pending BLOCK Sign-off."
            ))
        return {
            "type": "ir.actions.act_window",
            "name": _("Co-sign BLOCK"),
            "res_model": "iris.block.signoff.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_candidate_id": self.id},
        }

    def action_open_block_reject_wizard(self):
        """Open the sign-off rejection wizard for a pending BLOCK."""
        self.ensure_one()
        if self.state != "pending_block":
            raise UserError(_(
                "BLOCK sign-off rejection is only available while the "
                "candidate is Pending BLOCK Sign-off."
            ))
        return {
            "type": "ir.actions.act_window",
            "name": _("Reject BLOCK Sign-off"),
            "res_model": "iris.block.reject.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_candidate_id": self.id},
        }

    def _block_signoff(self, block_kind):
        """Co-sign the pending BLOCK: candidate → blocked, episode closed.

        Co-sign authority is ANY iris user — but never the proposer
        (manager-only would deadlock one-manager shops on their own
        manual BLOCKs).
        """
        self.ensure_one()
        if self.state != "pending_block":
            raise UserError(_(
                "BLOCK sign-off is only available while the candidate is "
                "Pending BLOCK Sign-off."
            ))
        if not self.env.user.has_group("iris.group_iris_user"):
            raise UserError(_("Only Iris users can sign off a BLOCK."))
        if block_kind not in _BLOCK_KINDS:
            raise UserError(_(
                "Pick the block kind (credibility or competence) — it "
                "selects the rejection letter."
            ))
        screening = self._get_pending_block_screening()
        if not screening:
            raise UserError(_("No screening awaiting BLOCK sign-off was found."))
        if self.env.user == screening.block_proposed_by_id:
            raise UserError(_(
                "The second sign-off must come from a different screener "
                "than the one who proposed the BLOCK."
            ))
        screening.sudo().write({
            "block_signoff_state": "approved",
            "block_signed_off_by_id": self.env.user.id,
            "block_signed_off_at": fields.Datetime.now(),
            "block_kind": block_kind,
        })
        self.sudo().write({"state": "blocked", "hold_deadline": False})
        self.activity_feedback(
            ["iris.mail_activity_type_block_signoff"],
            feedback=_("BLOCK co-signed by %s.") % self.env.user.name,
        )
        self.message_post(body=_(
            "BLOCK confirmed: proposed by %(proposer)s, signed off by "
            "%(cosigner)s — kind: %(kind)s.",
            proposer=screening.block_proposed_by_id.name or _("unknown"),
            cosigner=self.env.user.name,
            kind=block_kind,
        ))
        return True

    def _block_signoff_reject(self, reason):
        """Reject the pending sign-off → Needs Review (manager decides).

        The proposer MAY reject their own pending BLOCK (fail-safe
        direction: rejection routes to MORE scrutiny, not less). Writing
        ``llm_status='needs_review'`` on the screening is LOAD-BEARING:
        ``_apply_manual_verdict`` looks the record up by it.
        """
        self.ensure_one()
        if self.state != "pending_block":
            raise UserError(_(
                "BLOCK sign-off rejection is only available while the "
                "candidate is Pending BLOCK Sign-off."
            ))
        if not (reason or "").strip():
            raise UserError(_("A rejection reason is required."))
        if not self.env.user.has_group("iris.group_iris_user"):
            raise UserError(_("Only Iris users can reject a BLOCK sign-off."))
        screening = self._get_pending_block_screening()
        if not screening:
            raise UserError(_("No screening awaiting BLOCK sign-off was found."))
        screening.sudo().write({
            "block_signoff_state": "rejected",
            "block_signoff_rejection_reason": reason,
            "llm_status": "needs_review",
        })
        # hold_deadline deliberately untouched — the never-extended
        # invariant survives reject → HOLD cycles.
        self.sudo().write({"state": "needs_review"})
        self.activity_unlink(["iris.mail_activity_type_block_signoff"])
        self.message_post(body=_(
            "BLOCK sign-off rejected by %(user)s — candidate routed to "
            "Needs Review. Reason: %(reason)s",
            user=self.env.user.name, reason=reason,
        ))
        return True

    def _schedule_block_signoff_activity(self):
        """ONE sign-off activity on the candidate, assigned to a non-proposer.

        Assignee: the first iris manager who is not the proposer; falls
        back to the admin user. Deduplicated — a second call while an
        identical activity is open is a no-op.
        """
        self.ensure_one()
        activity_type = self.env.ref(
            "iris.mail_activity_type_block_signoff", raise_if_not_found=False,
        )
        if activity_type:
            existing = self.env["mail.activity"].sudo().search([
                ("res_model", "=", self._name),
                ("res_id", "=", self.id),
                ("activity_type_id", "=", activity_type.id),
            ], limit=1)
            if existing:
                return
        screening = self._get_pending_block_screening()
        proposer = screening.block_proposed_by_id
        manager_group = self.env.ref(
            "iris.group_iris_manager", raise_if_not_found=False,
        )
        candidates_pool = (
            manager_group.sudo().user_ids if manager_group
            else self.env["res.users"]
        )
        managers = candidates_pool.filtered(
            lambda u: u.active and u != proposer
        ).sorted("id")
        assignee = managers[:1] or self.env.ref(
            "base.user_admin", raise_if_not_found=False,
        ) or self.env.user
        self.activity_schedule(
            "iris.mail_activity_type_block_signoff",
            user_id=assignee.id,
            summary=_("Second-screener sign-off required on BLOCK"),
            note=_(
                "Screening #%(attempt)s proposed a BLOCK (by %(proposer)s). "
                "A DIFFERENT screener must co-sign it — or reject the "
                "sign-off with a reason — before the candidate is finally "
                "blocked.",
                attempt=screening.attempt or screening.id,
                proposer=proposer.name or _("unknown"),
            ),
        )
        self.message_post(
            body=_(
                "BLOCK proposed by %(proposer)s — awaiting second-screener "
                "sign-off (assigned to %(assignee)s).",
                proposer=proposer.name or _("unknown"),
                assignee=assignee.name,
            ),
            partner_ids=assignee.partner_id.ids,
        )

    # ------------------------------------------------------------------
    # Candidate communication (v1.1, P1-5 — manual composer send ONLY)
    # ------------------------------------------------------------------
    def action_send_hold_verification_email(self):
        """HOLD verification request (embeds clarifying questions if any)."""
        self.ensure_one()
        if self.state != "hold":
            raise UserError(_(
                "The verification request can only be sent while the "
                "candidate is on Hold."
            ))
        return self._open_mail_composer("iris.mail_template_iris_hold_verification")

    def action_send_interview_invite_email(self):
        """Interview invitation (shipped / interview ready)."""
        self.ensure_one()
        if self.state not in ("shipped", "interview_ready"):
            raise UserError(_(
                "The interview invitation can only be sent to a Shipped or "
                "Interview Ready candidate."
            ))
        return self._open_mail_composer("iris.mail_template_iris_ship_invite")

    def action_send_rejection_email(self):
        """Rejection letter — template COMPUTED from the block kind.

        The sender cannot pick the letter: a blocked candidate's letter is
        selected by the co-signed ``block_kind`` (and requires a completed
        second-screener sign-off — rule 8's "before it is communicated" as
        code); a rejected-after-scoring candidate always gets the
        competence letter.
        """
        self.ensure_one()
        if self.state not in ("blocked", "rejected"):
            raise UserError(_(
                "Rejection letters can only be sent to Blocked or Rejected "
                "candidates."
            ))
        if self.state == "blocked":
            screening = self.current_screening_id
            if screening.block_signoff_state != "approved":
                raise UserError(_(
                    "BLOCK communications require a completed "
                    "second-screener sign-off."
                ))
            kind = screening.block_kind
        else:
            kind = "competence"
        template_xmlid = (
            "iris.mail_template_iris_reject_credibility"
            if kind == "credibility"
            else "iris.mail_template_iris_reject_competence"
        )
        return self._open_mail_composer(template_xmlid)

    def _open_mail_composer(self, template_xmlid):
        """Shared mail.compose.message act_window (manual send only)."""
        self.ensure_one()
        if not (self.email or "").strip():
            raise UserError(_(
                "Set the candidate's email address before sending."
            ))
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        return {
            "type": "ir.actions.act_window",
            "name": _("Send Email"),
            "res_model": "mail.compose.message",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_model": "iris.candidate",
                "default_res_ids": [self.id],
                "default_template_id": template.id if template else False,
                "default_composition_mode": "comment",
            },
        }

    # ------------------------------------------------------------------
    # Clarifying questions (v1.1, P2-9 — passthrough to the screening)
    # ------------------------------------------------------------------
    def action_generate_clarifying_questions(self):
        """Generate candidate-facing questions for the active HOLD episode.

        Thin passthrough — guards and the LLM artifact live on
        ``iris.screening.action_generate_clarifying_questions``.
        """
        self.ensure_one()
        screening = self._get_current_hold_screening()
        if not screening:
            raise UserError(_(
                "No HOLD screening found — clarifying questions are only "
                "available for candidates on Hold."
            ))
        return screening.action_generate_clarifying_questions()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_current_hold_screening(self):
        """Latest screening with verdict=hold (the active hold episode)."""
        self.ensure_one()
        return self.screening_ids.filtered(
            lambda s: s.verdict == "hold"
        ).sorted("id")[-1:]

    def _get_pending_block_screening(self):
        """Latest BLOCK screening awaiting the second-screener sign-off."""
        self.ensure_one()
        return self.screening_ids.filtered(
            lambda s: s.verdict == "block" and s.block_signoff_state == "pending"
        ).sorted("id")[-1:]

    # ------------------------------------------------------------------
    # Crons
    # ------------------------------------------------------------------
    @api.model
    def _can_commit(self):
        """Commit/rollback is forbidden inside the test transaction."""
        return not (tools.config["test_enable"] or modules.module.current_test)

    @api.model
    def _cron_process_llm_queue(self, batch_size=10):
        """Process queued LLM jobs across all queue-registered models.

        Mirrors ``i2i_item._cron_run_pending_llm_qc``: each item runs
        synchronously with a commit after success and a rollback + log on
        crash, so one bad item never poisons the batch. Also reaps jobs
        stuck in ``running`` longer than 30 minutes (worker killed mid-call)
        by failing them through the normal failure path, and finishes with
        a safety sweep over screening batches whose members have all
        settled (in case the candidate write-hook trigger was missed).
        """
        # 1) Reap stale running jobs.
        stale_cutoff = fields.Datetime.subtract(
            fields.Datetime.now(), minutes=_LLM_RUNNING_REAP_MINUTES,
        )
        for model_name in _LLM_QUEUE_MODELS:
            stale = self.env[model_name].search([
                ("llm_status", "=", "running"),
                ("llm_started_at", "<", stale_cutoff),
            ])
            for rec in stale:
                try:
                    rec._llm_mark_failed(_(
                        "LLM call did not complete within %s minutes "
                        "(worker interrupted); marked as failed."
                    ) % _LLM_RUNNING_REAP_MINUTES)
                    if self._can_commit():
                        self.env.cr.commit()
                except Exception:  # noqa: BLE001
                    _logger.exception(
                        "Iris cron: failed reaping stale %s(%s)", model_name, rec.id,
                    )
                    if self._can_commit():
                        self.env.cr.rollback()

        # 2) Process queued jobs oldest-first across the registered models.
        queued = []
        for model_name in _LLM_QUEUE_MODELS:
            for rec in self.env[model_name].search(
                [("llm_status", "=", "queued")],
                order="create_date asc, id asc",
                limit=batch_size,
            ):
                queued.append((rec.create_date, model_name, rec.id))
        queued.sort(key=lambda item: (item[0], item[2]))

        for _create_date, model_name, rec_id in queued[:batch_size]:
            rec = self.env[model_name].browse(rec_id)
            try:
                rec._llm_run_sync()
                if self._can_commit():
                    self.env.cr.commit()
            except Exception:  # noqa: BLE001 — keep the batch alive
                _logger.exception(
                    "Iris cron: LLM job crashed for %s(%s)", model_name, rec_id,
                )
                if self._can_commit():
                    self.env.cr.rollback()

        # 3) Safety net: flip screening batches whose members have ALL
        #    settled but whose write-hook feeder was missed (crash between
        #    commits, manual data fixes, member unlink below viability).
        #    _check_members_settled is idempotent and state-guarded.
        batches = self.env["iris.screening.batch"].search([
            ("state", "=", "screening"),
        ])
        if batches:
            try:
                batches._check_members_settled()
                if self._can_commit():
                    self.env.cr.commit()
            except Exception:  # noqa: BLE001 — never poison the cron
                _logger.exception("Iris cron: batch settlement sweep failed")
                if self._can_commit():
                    self.env.cr.rollback()

    @api.model
    def _cron_auto_block_expired_holds(self):
        """Auto-BLOCK candidates whose hold deadline has passed (strict <)."""
        today = fields.Date.context_today(self)
        expired = self.search([
            ("state", "=", "hold"),
            ("hold_deadline", "<", today),
        ])
        for candidate in expired:
            deadline = candidate.hold_deadline
            hold_screening = candidate._get_current_hold_screening()
            if hold_screening:
                hold_screening.sudo().write({"auto_blocked": True})
            candidate.write({"state": "blocked", "hold_deadline": False})
            candidate.message_post(body=_(
                "Hold expired without verification evidence "
                "(deadline was %s) — candidate auto-blocked."
            ) % (deadline or _("unset")))
        if expired:
            _logger.info(
                "Iris cron: auto-blocked %d expired hold(s): %s",
                len(expired), expired.ids,
            )
