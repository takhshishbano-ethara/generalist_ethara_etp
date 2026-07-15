from odoo import models, fields, api
from odoo.exceptions import UserError


class HrApplicant(models.Model):
    _inherit = "hr.applicant"

    assessment_ids = fields.One2many(
        "etp.applicant.assessment",
        "applicant_id",
        string="Assessments",
    )
    assessment_count = fields.Integer(
        compute="_compute_assessment_count", store=False,
    )
    latest_assessment_id = fields.Many2one(
        "etp.applicant.assessment",
        compute="_compute_latest_assessment",
        store=False,
    )
    latest_assessment_state = fields.Selection(
        related="latest_assessment_id.state",
        string="Assessment State",
        store=False,
        readonly=True,
    )
    latest_assessment_result = fields.Selection(
        related="latest_assessment_id.result",
        string="Assessment Result",
        store=False,
        readonly=True,
    )

    @api.depends("assessment_ids")
    def _compute_assessment_count(self):
        for rec in self:
            rec.assessment_count = len(rec.assessment_ids)

    @api.depends("assessment_ids", "assessment_ids.create_date")
    def _compute_latest_assessment(self):
        for rec in self:
            rec.latest_assessment_id = (
                rec.assessment_ids.sorted("create_date", reverse=True)[:1]
            )

    def action_send_assessment(self):
        Assessment = self.env["etp.applicant.assessment"]
        created = self.env["etp.applicant.assessment"]
        skipped = []
        for applicant in self:
            job = applicant.job_id
            if not job or not job.assessment_template_id:
                skipped.append(applicant.partner_name or f"#{applicant.id}")
                continue
            active = Assessment.search([
                ("applicant_id", "=", applicant.id),
                ("state", "in", ("draft", "sent", "in_progress")),
            ], limit=1)
            if active:
                created |= active
                continue
            record = Assessment.create({
                "applicant_id": applicant.id,
                "job_id": job.id,
                "template_id": job.assessment_template_id.id,
            })
            record.action_send()
            created |= record

        if skipped:
            raise UserError(
                "The following applicants have no assessment template on their "
                "job position: " + ", ".join(skipped)
            )

        if len(created) == 1:
            return {
                "type": "ir.actions.act_window",
                "res_model": "etp.applicant.assessment",
                "view_mode": "form",
                "res_id": created.id,
            }
        return {
            "type": "ir.actions.act_window",
            "res_model": "etp.applicant.assessment",
            "view_mode": "list,form",
            "domain": [("id", "in", created.ids)],
        }

    def action_view_assessments(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Assessments — {self.partner_name or self.name}",
            "res_model": "etp.applicant.assessment",
            "view_mode": "list,form",
            "domain": [("applicant_id", "=", self.id)],
            "context": {
                "default_applicant_id": self.id,
                "default_job_id": self.job_id.id,
            },
        }
