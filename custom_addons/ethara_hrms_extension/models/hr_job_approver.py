from odoo import models, fields


class HrJobApprover(models.Model):
    _name = 'hr.job.approver'
    _description = 'Job Approver'
    _order = 'job_id, sequence, id'

    job_id = fields.Many2one('hr.job', string='Job', required=True, ondelete='cascade')
    sequence = fields.Integer(string='Sequence', default=10)
    email = fields.Char(string='Email', required=True)
    role = fields.Selection(
        [
            ('recipient', 'Recipient'),
            ('reviewer', 'Reviewer'),
        ],
        string='Role',
        default='recipient',
    )
