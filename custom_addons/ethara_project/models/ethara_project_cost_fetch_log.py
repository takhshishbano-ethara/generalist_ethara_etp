from odoo import api, fields, models


LOG_SOURCE_SELECTION = [
    ('manual_button', 'Manual (Form Button)'),
    ('cron', 'Scheduled'),
    ('other', 'Other'),
]

LOG_STATUS_SELECTION = [
    ('success', 'Success'),
    ('error', 'Error'),
]


class EtharaProjectCostFetchLog(models.Model):
    _name = 'ethara.project.cost.fetch.log'
    _description = 'Ethara Project Cost Fetch History'
    _order = 'fetched_at desc, id desc'
    _rec_name = 'display_name'

    budget_id = fields.Many2one(
        comodel_name='ethara.project.budget',
        required=True,
        ondelete='cascade',
        index=True,
    )
    ethara_project_id = fields.Many2one(
        related='budget_id.ethara_project_id',
        store=True,
        index=True,
        readonly=True,
    )
    fetched_at = fields.Datetime(
        required=True, index=True, default=fields.Datetime.now,
    )
    triggered_by_id = fields.Many2one(
        comodel_name='res.users', string='Triggered By', index=True,
    )

    source = fields.Selection(
        selection=LOG_SOURCE_SELECTION, required=True, default='other', index=True,
    )
    status = fields.Selection(
        selection=LOG_STATUS_SELECTION, required=True, default='success', index=True,
    )
    provider = fields.Selection(
        selection=[
            ('aws', 'AWS Cost Explorer'),
            ('openrouter', 'OpenRouter'),
            ('moonshot', 'Moonshot'),
            ('openai', 'OpenAI'),
            ('gcp', 'GCP BigQuery'),
        ],
        string='Provider',
        required=True,
        default='aws',
        index=True,
    )
    created_count = fields.Integer(string='Created Cost Lines')
    updated_count = fields.Integer(string='Updated Cost Lines')
    error_message = fields.Text()
    tags_summary = fields.Char(string='Tags (snapshot)')
    fetch_months = fields.Integer(string='Months Fetched (snapshot)')

    display_name = fields.Char(compute='_compute_display_name', store=False)

    @api.depends('budget_id.name', 'fetched_at', 'status', 'provider')
    def _compute_display_name(self):
        for rec in self:
            stamp = (
                fields.Datetime.to_string(rec.fetched_at) if rec.fetched_at else ''
            )
            rec.display_name = '%s - %s [%s / %s]' % (
                rec.budget_id.name or '?',
                stamp,
                rec.provider or '',
                rec.status or '',
            )
