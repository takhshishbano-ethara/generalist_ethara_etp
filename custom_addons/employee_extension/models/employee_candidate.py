from odoo import fields, models


class EmployeeCollege(models.Model):
    _name = 'employee.college'
    _description = 'College / University'
    _order = 'name asc'

    name = fields.Char(string='Name', required=True)
    code = fields.Char(string='Code')
    city = fields.Char(string='City')
    active = fields.Boolean(default=True)


class EmployeeCandidate(models.Model):
    _name = 'employee.candidate'
    _description = 'Self-Registered Candidate'
    _order = 'create_date desc'
    _rec_name = 'name'

    name = fields.Char(string='Full Name', required=True)
    gender = fields.Selection(
        [('male', 'Male'), ('female', 'Female'), ('other', 'Other')],
        string='Gender',
    )
    phone = fields.Char(string='Phone')
    personal_email = fields.Char(string='Personal Email', required=True, index=True)

    experience = fields.Selection(
        [('fresher', 'Fresher'), ('experienced', 'Experienced')],
        string='Experience',
        default='fresher',
        required=True,
    )
    experience_years = fields.Float(string='Experience Years')
    college_id = fields.Many2one('employee.college', string='College')

    aadhaar_number = fields.Char(string='Aadhaar Number', index=True)
    birthday = fields.Date(string='Date of Birth')

    aadhaar_card_url = fields.Char(string='Aadhaar Card URL')
    resume_url = fields.Char(string='Resume URL')

    user_id = fields.Many2one(
        'res.users', string='Portal User', ondelete='set null', readonly=True,
    )

    state = fields.Selection(
        [
            ('new', 'New'),
            ('shortlisted', 'Shortlisted'),
            ('interview', 'Interview'),
            ('hired', 'Hired'),
            ('rejected', 'Rejected'),
        ],
        default='new',
        string='Status',
        tracking=True,
    )

    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            'personal_email_unique',
            'unique(personal_email)',
            'A candidate with this personal email already exists.',
        ),
    ]
