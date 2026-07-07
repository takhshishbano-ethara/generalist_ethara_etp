from odoo import fields, models


class HrApplicantJobHistory(models.Model):
    _name = 'hr.applicant.job.history'
    _description = 'Applicant Job Change History'
    _order = 'changed_at desc, id desc'
    _rec_name = 'applicant_id'

    applicant_id = fields.Many2one(
        'hr.applicant', string='Applicant',
        required=True, ondelete='cascade', index=True,
    )
    previous_job_id = fields.Many2one(
        'hr.job', string='Previous Job', ondelete='set null',
    )
    new_job_id = fields.Many2one(
        'hr.job', string='New Job', ondelete='set null',
    )
    changed_by_id = fields.Many2one(
        'res.users', string='Changed By', default=lambda self: self.env.user,
    )
    changed_at = fields.Datetime(
        string='Changed At', default=fields.Datetime.now, required=True,
    )
    note = fields.Char(string='Note')
