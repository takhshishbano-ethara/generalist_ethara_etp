from odoo import models, fields, api


class WikiFeedback(models.Model):
    """Free-text feedback a portal user leaves on a wiki page
    ("Feedback on this page"). Submitted from the Flutter app via the
    /api/v1/wiki/feedback endpoint and reviewed in the Odoo backend."""
    _name = 'wiki.feedback'
    _description = 'Wiki Page Feedback'
    _order = 'create_date desc, id desc'

    reference = fields.Char(
        string='Reference', readonly=True, copy=False, default='New')
    page = fields.Char(
        string='Page',
        help='Key/route of the wiki page the feedback is about, e.g. "faqs".')
    page_label = fields.Char(string='Page Label')
    message = fields.Text(string='Feedback')
    helpful = fields.Selection(
        [('up', 'Yes'), ('down', 'No')], string='Was it helpful?',
        help='Result of the "Was this helpful?" vote on the page.')
    employee_id = fields.Many2one('hr.employee', string='Employee')
    user_id = fields.Many2one('res.users', string='User')
    state = fields.Selection(
        [('new', 'New'), ('reviewed', 'Reviewed'), ('closed', 'Closed')],
        string='Status', required=True, default='new')

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if not record.reference or record.reference == 'New':
                record.reference = 'FB-%04d' % record.id
        return records
