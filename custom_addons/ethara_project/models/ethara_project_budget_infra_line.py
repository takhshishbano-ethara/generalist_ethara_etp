from odoo import api, fields, models


class EtharaProjectBudgetInfraLine(models.Model):
    _name = 'ethara.project.budget.infra.line'
    _description = 'Ethara Project Budget Infrastructure Line'
    _order = 'id'

    budget_id = fields.Many2one(
        comodel_name='ethara.project.budget',
        string='Project Budget',
        required=True,
        ondelete='cascade',
        index=True,
    )
    infra_type_id = fields.Many2one(
        comodel_name='ethara.project.infra.type',
        string='Infrastructure',
        required=True,
    )
    description = fields.Char(string='Description')
    budget_amount = fields.Float(string='Budget (USD)')
    start_date = fields.Date(string='Start Date')
    end_date = fields.Date(string='End Date')
    per_day_cost = fields.Float(
        string='Per Day Cost (USD)',
        compute='_compute_per_day_cost',
        store=True,
    )

    instance_type = fields.Char(string='Instance Type')
    unit_price_usd = fields.Float(
        string='Compute Rate (USD/Hr)',
        digits=(16, 6),
    )
    price_unit = fields.Char(string='Price Unit')
    quantity = fields.Float(string='Quantity', default=1.0)
    duration_hours = fields.Float(string='Hours / Month', default=730.0)
    ebs_storage_gb = fields.Float(
        string='EBS Storage (GB)',
        digits=(16, 2),
    )
    volume_type = fields.Char(string='Volume Type', default='gp3')
    volume_rate_usd_per_gb_mo = fields.Float(
        string='Storage Rate (USD/GB-mo)',
        digits=(16, 6),
    )
    compute_amount = fields.Float(
        string='Compute (USD)',
        compute='_compute_compute_amount',
        store=True,
        digits=(16, 2),
    )
    storage_amount = fields.Float(
        string='Storage (USD)',
        compute='_compute_storage_amount',
        store=True,
        digits=(16, 2),
    )
    computed_amount = fields.Float(
        string='Computed Total (USD)',
        compute='_compute_computed_amount',
        store=True,
        digits=(16, 2),
    )

    @api.depends('budget_amount')
    def _compute_per_day_cost(self):
        for rec in self:
            rec.per_day_cost = (rec.budget_amount or 0.0) / 30.0

    @api.depends('unit_price_usd', 'quantity', 'duration_hours')
    def _compute_compute_amount(self):
        for rec in self:
            rec.compute_amount = (
                (rec.unit_price_usd or 0.0)
                * (rec.quantity or 0.0)
                * (rec.duration_hours or 0.0)
            )

    @api.depends('ebs_storage_gb', 'volume_rate_usd_per_gb_mo', 'quantity')
    def _compute_storage_amount(self):
        for rec in self:
            rec.storage_amount = (
                (rec.ebs_storage_gb or 0.0)
                * (rec.volume_rate_usd_per_gb_mo or 0.0)
                * (rec.quantity or 0.0)
            )

    @api.depends('compute_amount', 'storage_amount')
    def _compute_computed_amount(self):
        for rec in self:
            rec.computed_amount = (rec.compute_amount or 0.0) + (
                rec.storage_amount or 0.0
            )
