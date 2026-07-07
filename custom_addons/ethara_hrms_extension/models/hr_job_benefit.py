from odoo import models, fields


class HrJobBenefit(models.Model):
    _name = 'hr.job.benefit'
    _description = 'Job Benefit'
    _order = 'job_id, sequence, id'

    job_id = fields.Many2one('hr.job', string='Job', required=True, ondelete='cascade')
    sequence = fields.Integer(string='Sequence', default=10)
    name = fields.Char(string='Benefit', required=True)
