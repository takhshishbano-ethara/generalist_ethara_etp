from odoo import fields, models


class EmployeeCollege(models.Model):
    _name = 'employee.college'
    _description = 'College / University'
    _order = 'name asc'

    name = fields.Char(string='Name', required=True)
    code = fields.Char(string='Code')
    city = fields.Char(string='City')
    active = fields.Boolean(default=True)


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'

    experience = fields.Selection(
        [('fresher', 'Fresher'), ('experienced', 'Experienced')],
        string='Experience',
        default='fresher',
    )
    experience_years = fields.Float(string='Experience Years')
    college_id = fields.Many2one('employee.college', string='College')

    gender = fields.Selection(
        [('male', 'Male'), ('female', 'Female'), ('other', 'Other')],
        string='Gender',
    )
    birthday = fields.Date(string='Date of Birth')
    aadhaar_number = fields.Char(string='Aadhaar Number', index=True)
    aadhaar_card_url = fields.Char(string='Aadhaar Card URL')
    resume_url = fields.Char(string='Resume URL')

    candidate_user_id = fields.Many2one(
        'res.users',
        string='Portal User',
        ondelete='set null',
        readonly=True,
        copy=False,
        index='btree_not_null',
    )

    _sql_constraints = [
        (
            'aadhaar_number_unique',
            'unique(aadhaar_number)',
            'A candidate with this Aadhaar number already exists.',
        ),
    ]
