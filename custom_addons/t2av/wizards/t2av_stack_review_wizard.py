"""T2AV Stack Review Wizard — atomic claim + Pass/Fail verdict.

A single TransientModel form used by the human reviewer to:
  1. Atomically claim the next queued human-review task (FIFO, single
     global queue, ``provider='human'`` + ``state='queued'`` +
     ``assigned_to_id IS NULL``).
  2. Watch the video and capture Pass/Fail plus mandatory free-form
     notes.
  3. On *Save & Next*, persist the verdict via
     ``t2av.video.review._apply_reviewer_verdict`` and chain straight to
     the next task without leaving the form. *Cancel* atomically
     releases the lock back to the queue.

Concurrency contract
--------------------
Claim is a two-statement transaction on ``t2av_video_review``:

  a. ``SELECT ... FOR UPDATE SKIP LOCKED`` picks one queued, unassigned
     row in FIFO order. ``SKIP LOCKED`` prevents reviewers from
     blocking each other on the head-of-queue row.
  b. A compare-and-set ``UPDATE ... WHERE state='queued' AND
     assigned_to_id IS NULL RETURNING id`` transitions the row to
     ``state='assigned'`` and stamps the lock holder. If RETURNING is
     empty the row was claimed between our SELECT and UPDATE — we
     retry up to a small bound, then surface a friendly notification.

Ownership contract
------------------
Only the assigned reviewer (or ``base.group_system``) may submit a
verdict; this is enforced inside
``t2av.video.review._apply_reviewer_verdict``. Cancel atomically
releases the lock (``state='assigned'`` + ``assigned_to_id=user`` →
``state='queued'`` + ``assigned_to_id=NULL``) so another reviewer can
pick the task up immediately.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

_CLAIM_MAX_RETRIES = 5


class T2AVStackReviewWizard(models.TransientModel):
    _name = "t2av.stack.review.wizard"
    _description = "T2AV Human Review Stack Wizard"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    review_id = fields.Many2one(
        "t2av.video.review",
        string="Review",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    attempt_id = fields.Many2one(
        related="review_id.attempt_id",
        string="Attempt",
        readonly=True,
    )
    job_id = fields.Many2one(
        related="review_id.attempt_id.job_id",
        string="Generation Job",
        readonly=True,
    )
    video_url = fields.Char(
        string="Video URL",
        compute="_compute_video_url",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Job-level snapshot (readonly related fields surfaced in the form)
    # ------------------------------------------------------------------
    category = fields.Selection(related="job_id.category", readonly=True)
    sub_category = fields.Char(related="job_id.sub_category", readonly=True)
    style = fields.Selection(related="job_id.style", readonly=True)
    priority = fields.Selection(related="job_id.priority", readonly=True)
    complexity = fields.Selection(related="job_id.complexity", readonly=True)
    duration = fields.Selection(related="job_id.duration", readonly=True)
    resolution = fields.Selection(related="job_id.resolution", readonly=True)
    aspect_ratio = fields.Selection(related="job_id.aspect_ratio", readonly=True)
    fps = fields.Float(related="attempt_id.fps", readonly=True)
    speaker_count = fields.Integer(related="job_id.speaker_count", readonly=True)
    language = fields.Char(related="job_id.language", readonly=True)
    topic = fields.Char(related="job_id.topic", readonly=True)
    prompt = fields.Text(related="job_id.prompt", readonly=True)
    enriched_prompt = fields.Text(related="job_id.enriched_prompt", readonly=True)
    golden_prompt = fields.Text(related="job_id.golden_prompt", readonly=True)

    # ------------------------------------------------------------------
    # Reviewer inputs (the only editable fields)
    # ------------------------------------------------------------------
    qc_status = fields.Selection(
        [("pass", "Pass"), ("fail", "Fail")],
        string="QC Status",
        help="Pass = video is acceptable. Fail = video needs regeneration.",
    )
    reviewer_notes = fields.Text(
        string="Reviewer Notes",
        help="Mandatory free-form explanation of the Pass/Fail decision. "
             "Captured verbatim on the review record and posted to chatter.",
    )

    # ------------------------------------------------------------------
    # Computes / constraints
    # ------------------------------------------------------------------
    @api.depends("attempt_id.video_play_url", "attempt_id.video_s3_url")
    def _compute_video_url(self):
        for rec in self:
            att = rec.attempt_id
            rec.video_url = (att.video_play_url or att.video_s3_url) if att else ""

    @api.constrains("reviewer_notes")
    def _check_reviewer_notes(self):
        for rec in self:
            if rec.reviewer_notes and not rec.reviewer_notes.strip():
                raise ValidationError(_(
                    "Reviewer notes are required — explain why the video passed or failed."
                ))

    # ------------------------------------------------------------------
    # Authorization helper
    # ------------------------------------------------------------------
    @api.model
    def _check_reviewer_access(self):
        user = self.env.user
        if user.has_group("base.group_system"):
            return
        if user.has_group("t2av.group_t2av_reviewer"):
            return
        raise UserError(_(
            "Only members of the T2AV Reviewer group may open the Stack."
        ))

    # ------------------------------------------------------------------
    # Atomic claim — pull next queued human-review task
    # ------------------------------------------------------------------
    @api.model
    def open_next(self):
        self._check_reviewer_access()
        Review = self.env["t2av.video.review"].sudo()
        user_id = self.env.user.id
        claimed = None

        for _attempt in range(_CLAIM_MAX_RETRIES):
            self.env.cr.execute(
                """
                SELECT id FROM t2av_video_review
                 WHERE provider       = 'human'
                   AND state          = 'queued'
                   AND assigned_to_id IS NULL
                 ORDER BY create_date ASC, id ASC
                 LIMIT 1
                 FOR UPDATE SKIP LOCKED
                """
            )
            row = self.env.cr.fetchone()
            if not row:
                return self._notify_queue_empty()
            candidate_id = row[0]

            now = fields.Datetime.now()
            self.env.cr.execute(
                """
                UPDATE t2av_video_review
                   SET state          = 'assigned',
                       assigned_to_id = %s,
                       locked_at      = %s
                 WHERE id             = %s
                   AND state          = 'queued'
                   AND assigned_to_id IS NULL
                RETURNING id
                """,
                (user_id, now, candidate_id),
            )
            if self.env.cr.fetchone():
                claimed = Review.browse(candidate_id)
                claimed.invalidate_recordset(
                    ["state", "assigned_to_id", "locked_at"]
                )
                break

        if not claimed:
            return self._notify_queue_contended()

        wizard = self.create({"review_id": claimed.id})
        return {
            "type": "ir.actions.act_window",
            "name": _("T2AV Stack — Review #%s") % claimed.id,
            "res_model": self._name,
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "current",
            "context": dict(self.env.context),
        }

    @api.model
    def _notify_queue_empty(self):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Stack empty"),
                "message": _("No human-review tasks are waiting. Great job!"),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    @api.model
    def _notify_queue_contended(self):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Stack busy"),
                "message": _(
                    "Another reviewer just claimed the next task. "
                    "Open the Stack again to try the following one."
                ),
                "type": "warning",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    # ------------------------------------------------------------------
    # Verdict + chain
    # ------------------------------------------------------------------
    def _save_verdict(self):
        self.ensure_one()
        if not self.qc_status:
            raise ValidationError(_("Select Pass or Fail before saving."))
        clean_notes = (self.reviewer_notes or "").strip()
        if not clean_notes:
            raise ValidationError(_(
                "Reviewer notes are required — explain why the video passed or failed."
            ))

        verdict = "accept" if self.qc_status == "pass" else "reject"
        review = self.review_id
        review_id = review.id
        user = self.env.user
        _logger.info(
            "T2AV Stack: user=%s submitting verdict=%s for review=%s "
            "(state=%s, assigned_to=%s)",
            user.login, verdict, review_id, review.state,
            review.assigned_to_id.login if review.assigned_to_id else None,
        )

        review._apply_reviewer_verdict(verdict, clean_notes)

        review.invalidate_recordset(["state", "verdict", "reviewer_notes"])
        actual_state = review.state
        actual_verdict = review.verdict
        if actual_state != "done" or actual_verdict != verdict:
            _logger.error(
                "T2AV Stack: save did NOT persist for review=%s "
                "(actual state=%s, verdict=%s; expected done/%s)",
                review_id, actual_state, actual_verdict, verdict,
            )
            raise UserError(_(
                "Verdict did not save (state=%(s)s, verdict=%(v)s). "
                "Refresh and try again, or contact an administrator."
            ) % {"s": actual_state, "v": actual_verdict})

        _logger.info(
            "T2AV Stack: review=%s saved (verdict=%s) by user=%s",
            review_id, verdict, user.login,
        )
        return verdict

    def action_save(self):
        self._save_verdict()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Verdict saved"),
                "message": _("Your review has been saved."),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def action_save_next(self):
        self._save_verdict()
        next_action = self.env[self._name].open_next()
        if next_action.get("type") == "ir.actions.client":
            params = next_action.setdefault("params", {})
            original_msg = params.get("message") or _("Your review has been saved.")
            params["title"] = _("Verdict saved")
            params["message"] = original_msg
            params["sticky"] = True
            params["type"] = "success"
        return next_action

    def action_cancel(self):
        self.ensure_one()
        user_id = self.env.user.id
        review_id = self.review_id.id
        self.env.cr.execute(
            """
            UPDATE t2av_video_review
               SET state          = 'queued',
                   assigned_to_id = NULL,
                   locked_at      = NULL
             WHERE id             = %s
               AND state          = 'assigned'
               AND assigned_to_id = %s
            RETURNING id
            """,
            (review_id, user_id),
        )
        released = bool(self.env.cr.fetchone())
        if released:
            self.review_id.invalidate_recordset(
                ["state", "assigned_to_id", "locked_at"]
            )
        _logger.info(
            "T2AV Stack: cancel by user=%s on review=%s (released=%s)",
            self.env.user.login, review_id, released,
        )
        return {
            "type": "ir.actions.client",
            "tag": "home",
        }

    # ------------------------------------------------------------------
    # Lock release safety net — TransientModel autovacuum / dismissal
    # ------------------------------------------------------------------
    def unlink(self):
        # If a wizard is being garbage-collected by TransientModel autovacuum,
        # or unlinked via any path other than action_cancel/action_save_next,
        # release the underlying review lock atomically so the task returns
        # to the queue. We key on the wizard's create_uid (the reviewer who
        # claimed it) — not env.user, which during autovacuum is superuser.
        snapshots = []
        for rec in self:
            if rec.review_id and rec.create_uid:
                snapshots.append((rec.review_id.id, rec.create_uid.id))
        for review_id, owner_uid in snapshots:
            try:
                self.env.cr.execute(
                    """
                    UPDATE t2av_video_review
                       SET state          = 'queued',
                           assigned_to_id = NULL,
                           locked_at      = NULL
                     WHERE id             = %s
                       AND state          = 'assigned'
                       AND assigned_to_id = %s
                    RETURNING id
                    """,
                    (review_id, owner_uid),
                )
                if self.env.cr.fetchone():
                    self.env["t2av.video.review"].browse(
                        review_id
                    ).invalidate_recordset(
                        ["state", "assigned_to_id", "locked_at"]
                    )
            except Exception:  # noqa: BLE001 — autovacuum must never crash
                _logger.exception(
                    "T2AV Stack: failed to release lock for review %s on unlink",
                    review_id,
                )
        return super().unlink()
