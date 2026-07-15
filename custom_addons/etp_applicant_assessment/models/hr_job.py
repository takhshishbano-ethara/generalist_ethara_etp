from odoo import models, fields


class HrJob(models.Model):
    _inherit = "hr.job"

    assessment_template_id = fields.Many2one(
        "etp.applicant.assessment.template",
        string="Assessment Template",
        help="Applied to every applicant who reaches the assessment stage.",
    )
    assessment_auto_send = fields.Boolean(
        string="Auto-send on Assessment Stage",
        default=False,
        help=(
            "When on, the assessment invitation email is sent automatically "
            "once an hr.applicant lands on this job's assessment stage. When "
            "off, HR must click 'Send Assessment' manually."
        ),
    )
    assessment_count = fields.Integer(
        compute="_compute_assessment_count",
        string="Assessments Sent",
    )

    def _compute_assessment_count(self):
        Assessment = self.env["etp.applicant.assessment"]
        for job in self:
            job.assessment_count = Assessment.search_count(
                [("job_id", "=", job.id)]
            )

    def action_view_assessments(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Assessments — {self.name}",
            "res_model": "etp.applicant.assessment",
            "view_mode": "list,form",
            "domain": [("job_id", "=", self.id)],
            "context": {"default_job_id": self.id},
        }
