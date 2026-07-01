from odoo import models, fields, api


class WikiGrievance(models.Model):
    _name = 'wiki.grievance'
    _description = 'Wiki Grievance'
    _order = 'create_date desc, id desc'

    CATEGORIES = [
        ('harassment', 'Harassment / POSH'),
        ('workplace', 'Workplace / facilities'),
        ('payroll', 'Payroll / benefits'),
        ('management', 'Management / team'),
        ('other', 'Other'),
    ]

    reference = fields.Char(string='Reference', readonly=True, copy=False,
                            default='New')
    category = fields.Selection(CATEGORIES, string='Category', required=True)
    description = fields.Text(string='Description', required=True)
    is_anonymous = fields.Boolean(string='Anonymous', default=False)
    employee_id = fields.Many2one('hr.employee', string='Employee')
    state = fields.Selection(
        [('draft', 'Draft'), ('submitted', 'Submitted'),
         ('in_review', 'In Review'), ('resolved', 'Resolved')],
        string='Status', required=True, default='draft')
    attachment_ids = fields.One2many(
        'ir.attachment', 'res_id', string='Evidence',
        domain=[('res_model', '=', 'wiki.grievance')])

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if not record.reference or record.reference == 'New':
                record.reference = 'GRV-%04d' % record.id
        return records
