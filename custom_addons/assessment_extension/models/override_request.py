# -*- coding: utf-8 -*-
"""Pending self-case score-override request — the SCR-098 CTO inbox.

WORKFLOW §6.5 (Self-case guard), §13 (Permission matrix). When a PL clicks
[Override score] for their own direct report, the override doesn't commit —
it lands here as a pending request for the CTO (or HR Admin) to approve.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EtpAssessmentOverrideRequest(models.Model):
    _name = "etp.assessment.override.request"
    _description = "Pending self-case score-override request (CTO sign-off)"
    _order = "requested_at desc"
    _rec_name = "code"

    code = fields.Char(
        string="Request Code",
        copy=False,
        index=True,
        help="Stable mono id (REQ-#####).",
    )

    submission_id = fields.Many2one(
        "etp.assessment.submission",
        required=True,
        ondelete="cascade",
        index=True,
    )
    assessment_id = fields.Many2one(
        related="submission_id.assessment_id", store=True, readonly=True
    )
    question_id = fields.Many2one(
        related="submission_id.question_id", store=True, readonly=True
    )
    candidate_employee_id = fields.Many2one(
        related="submission_id.employee_id", store=True, readonly=True
    )

    llm_score = fields.Integer(
        string="LLM score",
        help="The score at request time (so a later auto-score change can be detected).",
    )
    requested_score = fields.Integer(string="Requested score", required=True)
    requested_reason = fields.Selection(
        [
            ("llm_synonym", "LLM penalized a correct synonym"),
            ("misdetected_boxes", "Mis-detected boxes"),
            ("justification_valid", "Justification valid"),
            ("other", "Other"),
        ],
        string="Requested reason",
        required=True,
    )
    requested_note = fields.Text(string="Note")
    item_result = fields.Selection(
        [("auto", "Auto"), ("pass", "Pass"), ("fail", "Fail")],
        default="auto",
    )

    requesting_user_id = fields.Many2one(
        "res.users",
        string="Requesting PL",
        required=True,
        default=lambda self: self.env.user,
    )
    requested_at = fields.Datetime(
        string="Requested at",
        default=lambda self: fields.Datetime.now(),
        required=True,
    )

    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        default="pending",
        required=True,
    )
    decided_by = fields.Many2one("res.users", string="Decided by")
    decided_at = fields.Datetime(string="Decided at")
    decision_note = fields.Text(string="Decision note")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if not record.code:
                seq = self.env["ir.sequence"].next_by_code(
                    "etp.assessment.override.request"
                ) or "REQ-%05d" % record.id
                record.code = seq
        return records

    def action_approve(self, note=None):
        """CTO approves → commit the override on the underlying submission."""
        for rec in self:
            if rec.state != "pending":
                raise UserError(_("Only pending requests can be approved."))
            sub = rec.submission_id
            sub.write(
                {
                    "override_score": rec.requested_score,
                    "override_by": self.env.user.id,
                    "override_at": fields.Datetime.now(),
                    "override_reason": rec.requested_reason,
                    "override_note": rec.requested_note or False,
                    "item_result": rec.item_result or "auto",
                    "state": "overridden",
                }
            )
            rec.write(
                {
                    "state": "approved",
                    "decided_by": self.env.user.id,
                    "decided_at": fields.Datetime.now(),
                    "decision_note": note or False,
                }
            )
        return True

    def action_reject(self, note=None):
        """CTO rejects → keep the LLM score; record audit trail."""
        for rec in self:
            if rec.state != "pending":
                raise UserError(_("Only pending requests can be rejected."))
            rec.write(
                {
                    "state": "rejected",
                    "decided_by": self.env.user.id,
                    "decided_at": fields.Datetime.now(),
                    "decision_note": note or False,
                }
            )
        return True
