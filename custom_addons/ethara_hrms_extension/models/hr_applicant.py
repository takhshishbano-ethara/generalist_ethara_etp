from odoo import api, fields, models


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'

    candidate_id = fields.Many2one(
        'ethara.candidate', string='Candidate',
        ondelete='set null', index=True,
    )
    portfolio_url = fields.Char(string='Portfolio URL')
    github_url = fields.Char(string='GitHub URL')
    resume_url = fields.Char(string='Resume URL')
    cancellation_reason = fields.Text(string='Cancellation Reason')
    status_updated_at = fields.Datetime(string='Status Updated At')

    job_history_ids = fields.One2many(
        'hr.applicant.job.history', 'applicant_id',
        string='Job Change History',
    )

    def write(self, vals):
        if 'job_id' in vals:
            History = self.env['hr.applicant.job.history'].sudo()
            new_job_id = vals.get('job_id') or False
            for applicant in self:
                old_job_id = applicant.job_id.id if applicant.job_id else False
                if old_job_id != new_job_id:
                    History.create({
                        'applicant_id': applicant.id,
                        'previous_job_id': old_job_id,
                        'new_job_id': new_job_id,
                        'changed_by_id': self.env.uid,
                    })
        return super().write(vals)

    @api.onchange('candidate_id', 'active', 'stage_id')
    def _onchange_warn_active_application(self):
        if not self.candidate_id or not self.active:
            return
        if self.stage_id and self.stage_id.hired_stage:
            return
        if self.refuse_reason_id:
            return
        domain = [
            ('candidate_id', '=', self.candidate_id.id),
            ('active', '=', True),
            ('refuse_reason_id', '=', False),
        ]
        if isinstance(self.id, int):
            domain.append(('id', '!=', self.id))
        others = self.env['hr.applicant'].sudo().search(domain).filtered(
            lambda a: not (a.stage_id and a.stage_id.hired_stage)
        )
        if others:
            return {
                'warning': {
                    'title': "Existing Application",
                    'message': (
                        "Candidate '%s' already has an active application "
                        "(id %s) for job '%s'. Cancel it before applying to "
                        "another job."
                    ) % (
                        self.candidate_id.name,
                        others[0].id,
                        others[0].job_id.name or '?',
                    ),
                }
            }
