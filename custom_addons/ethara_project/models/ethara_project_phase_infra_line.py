from odoo import api, fields, models


class EtharaProjectPhaseInfraLine(models.Model):
    _name = 'ethara.project.phase.infra.line'
    _description = 'Ethara Project Phase Infrastructure Line'
    _order = 'id'

    phase_id = fields.Many2one(
        comodel_name='ethara.project.phase',
        string='Phase',
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
    approved_amount = fields.Float(string='Approved (USD)')
    start_date = fields.Date(string='Start Date')
    end_date = fields.Date(string='End Date')
    per_day_cost = fields.Float(
        string='Per Day Cost (USD)',
        compute='_compute_per_day_cost',
        store=True,
    )
    instance_type = fields.Char(string='Instance Type')
    unit_price_usd = fields.Float(string='Compute Rate (USD/Hr)', digits=(16, 6))
    price_unit = fields.Char(string='Price Unit')
    quantity = fields.Float(string='Quantity', default=1.0)
    duration_hours = fields.Float(string='Hours / Month', default=730.0)
    ebs_storage_gb = fields.Float(string='EBS Storage (GB)', digits=(16, 2))
    volume_type = fields.Char(string='Volume Type', default='gp3')
    volume_rate_usd_per_gb_mo = fields.Float(
        string='Storage Rate (USD/GB-mo)', digits=(16, 6),
    )

    @api.depends('budget_amount')
    def _compute_per_day_cost(self):
        for rec in self:
            rec.per_day_cost = (rec.budget_amount or 0.0) / 30.0
