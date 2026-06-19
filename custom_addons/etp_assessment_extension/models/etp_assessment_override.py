# -*- coding: utf-8 -*-
"""Override approval requests raised against a candidate's submission.

Used by the SCR-098 override approvals queue: a PL (program lead) raises
an override against a single candidate's run (e.g. "LLM mis-read" or
"self-case anomaly"), an assessment manager approves or rejects it, and
the result is recorded with a decision note for the audit trail.
"""

from odoo import api, fields, models


class EtpAssessmentOverride(models.Model):
    _name = "etp.assessment.override"
    _description = "ETP Assessment Override Request"
    _order = "raised_at desc, id desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    code = fields.Char(
        string="Code",
        readonly=True,
        copy=False,
        default=lambda self: "New",
    )
    assessment_id = fields.Many2one(
        "etp.assessment",
        string="Assessment",
        required=True,
        ondelete="cascade",
        index=True,
    )
    evaluator_id = fields.Many2one(
        "etp.assessment.evaluator",
        string="Candidate run",
        ondelete="set null",
        index=True,
    )
    candidate_id = fields.Many2one(
        "hr.applicant",
        related="evaluator_id.applicant_id",
        readonly=True,
        string="Candidate",
    )
    override_type = fields.Selection(
        selection=[
            ("self_case", "Self-case anomaly"),
            ("llm_misread", "LLM mis-read"),
            ("scoring_dispute", "Scoring dispute"),
            ("technical_issue", "Technical issue"),
            ("other", "Other"),
        ],
        default="other",
        required=True,
        string="Type",
    )
    reason = fields.Text(string="Reason", required=True)
    requester_id = fields.Many2one(
        "res.users",
        string="Raised by",
        required=True,
        default=lambda self: self.env.user,
    )
    pl_id = fields.Many2one(
        "res.users",
        string="Program lead",
    )
    raised_at = fields.Datetime(
        string="Raised at",
        default=fields.Datetime.now,
        required=True,
        readonly=True,
    )
    state = fields.Selection(
        selection=[
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        default="pending",
        required=True,
        tracking=True,
    )
    decision_at = fields.Datetime(string="Decided at", readonly=True)
    decided_by_id = fields.Many2one(
        "res.users",
        string="Decided by",
        readonly=True,
    )
    decision_notes = fields.Text(string="Decision notes")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("code", "New") == "New":
                seq = self.env["ir.sequence"].next_by_code(
                    "etp.assessment.override"
                )
                vals["code"] = seq or self._fallback_code()
        return super().create(vals_list)

    @api.model
    def _fallback_code(self):
        last = self.search([], order="id desc", limit=1)
        next_no = (last.id + 1) if last else 1
        return f"OVR-{next_no:03d}"

    def action_approve(self, decision_notes=False):
        for rec in self:
            if rec.state != "pending":
                continue
            rec.write({
                "state": "approved",
                "decision_at": fields.Datetime.now(),
                "decided_by_id": self.env.user.id,
                "decision_notes": decision_notes or rec.decision_notes,
            })
            rec.message_post(
                body=f"Override approved by {self.env.user.name}.",
            )
        return True

    def action_reject(self, decision_notes=False):
        for rec in self:
            if rec.state != "pending":
                continue
            rec.write({
                "state": "rejected",
                "decision_at": fields.Datetime.now(),
                "decided_by_id": self.env.user.id,
                "decision_notes": decision_notes or rec.decision_notes,
            })
            rec.message_post(
                body=f"Override rejected by {self.env.user.name}.",
            )
        return True
