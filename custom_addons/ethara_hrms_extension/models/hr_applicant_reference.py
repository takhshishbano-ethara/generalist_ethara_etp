from odoo import fields, models


class HrApplicantReference(models.Model):
    _name = 'hr.applicant.reference'
    _description = 'Applicant Professional Reference'
    _order = 'sequence, id'

    applicant_id = fields.Many2one(
        'hr.applicant', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string='Full Name', required=True)
    email = fields.Char(string='Email', required=True)
    phone = fields.Char(string='Phone', required=True)
    linkedin_url = fields.Char(string='LinkedIn Profile')
