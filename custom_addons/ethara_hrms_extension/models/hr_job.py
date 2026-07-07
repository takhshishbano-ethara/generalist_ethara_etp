from odoo import models, fields


class HrJob(models.Model):
    _inherit = 'hr.job'

    slug = fields.Char(string='Slug', index=True, copy=False)
    summary = fields.Text(string='Summary')
    job_location = fields.Char(string='Job Location')

    employment_type = fields.Selection(
        [
            ('full_time', 'Full-time'),
            ('part_time', 'Part-time'),
            ('contract', 'Contract'),
            ('internship', 'Internship'),
            ('freelance', 'Freelance'),
        ],
        string='Employment Type',
    )
    work_mode = fields.Selection(
        [
            ('on_site', 'On-site'),
            ('remote', 'Remote'),
            ('hybrid', 'Hybrid'),
        ],
        string='Work Mode',
    )
    experience_level = fields.Selection(
        [
            ('fresher', 'Fresher (0-1 yr)'),
            ('junior', 'Junior (1-3 yrs)'),
            ('mid', 'Mid (3-6 yrs)'),
            ('senior', 'Senior (6-10 yrs)'),
            ('lead', 'Lead (10+ yrs)'),
        ],
        string='Experience Level',
    )
    experience_years = fields.Float(string='Experience Years')
    salary_bracket = fields.Char(string='Salary Bracket')

    responsibility_ids = fields.One2many(
        'hr.job.responsibility', 'job_id', string='Responsibilities'
    )
    benefit_ids = fields.One2many(
        'hr.job.benefit', 'job_id', string='Benefits'
    )
    preferred_skill_ids = fields.Many2many(
        'hr.skill', 'hr_job_preferred_skill_rel', 'job_id', 'skill_id',
        string='Preferred Skills',
    )

    is_featured = fields.Boolean(string='Featured')
    posted_at = fields.Datetime(string='Posted At')
    urgency_level = fields.Selection(
        [
            ('1', 'Low'),
            ('2', 'Normal'),
            ('3', 'Elevated'),
            ('4', 'High'),
            ('5', 'Critical'),
        ],
        string='Urgency Level',
    )
    screening_prompt = fields.Text(string='Screening Prompt')

    approval_status = fields.Selection(
        [
            ('draft', 'Draft'),
            ('requested', 'Requested'),
            ('posted', 'Posted'),
            ('rejected', 'Rejected'),
            ('withdrawn', 'Withdrawn'),
        ],
        string='Approval Status',
        default='draft',
        tracking=True,
    )
    approval_requested_at = fields.Datetime(string='Approval Requested At')
    approval_decided_at = fields.Datetime(string='Approval Decided At')
    approval_email_sent_at = fields.Datetime(string='Approval Email Sent At')

    requested_by_id = fields.Many2one('res.users', string='Requested By')
    external_requested_by = fields.Char(string='External Requested By')
    approved_by_id = fields.Many2one('res.users', string='Approved By')

    approver_ids = fields.One2many(
        'hr.job.approver', 'job_id', string='Approvers'
    )
    approval_recipient_email = fields.Char(string='Approval Recipient Emails')
    reviewed_by_email = fields.Char(string='Reviewed By Email')
    rejection_reason = fields.Text(string='Rejection Reason')

    _sql_constraints = [
        ('slug_unique', 'unique(slug)', 'Job slug must be unique.'),
    ]
