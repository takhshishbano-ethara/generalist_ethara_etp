from odoo import models, fields


class HrJobResponsibility(models.Model):
    _name = 'hr.job.responsibility'
    _description = 'Job Responsibility'
    _order = 'job_id, sequence, id'

    job_id = fields.Many2one('hr.job', string='Job', required=True, ondelete='cascade')
    sequence = fields.Integer(string='Sequence', default=10)
    name = fields.Text(string='Responsibility', required=True)
