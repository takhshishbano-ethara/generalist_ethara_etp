from odoo import api, fields, models


class EtharaProjectPhaseSubscriptionLine(models.Model):
    _name = 'ethara.project.phase.subscription.line'
    _description = 'Ethara Project Phase Subscription Line'
    _order = 'id'

    phase_id = fields.Many2one(
        comodel_name='ethara.project.phase',
        string='Phase',
        required=True,
        ondelete='cascade',
        index=True,
    )
    subscription_id = fields.Many2one(
        comodel_name='ethara.project.subscription',
        string='Subscription',
        required=True,
        ondelete='restrict',
    )
    name = fields.Char(
        string='Subscription Name',
        related='subscription_id.name',
        store=True,
        readonly=True,
    )
    cost_per_subscription = fields.Float(
        related='subscription_id.cost',
        store=True,
        readonly=True,
    )
    assigned_user_ids = fields.Many2many(
        comodel_name='res.users',
        relation='ethara_project_phase_sub_line_user_rel',
        column1='sub_line_id',
        column2='user_id',
        string='Assigned To',
    )
    subscription_count = fields.Integer(
        compute='_compute_subscription_count',
        store=True,
    )
    final_amount = fields.Float(
        string='Monthly Cost (USD)',
        compute='_compute_final_amount',
        store=True,
    )
    approved_amount = fields.Float(string='Approved (USD)')
    per_day_cost = fields.Float(
        string='Per Day Cost (USD)',
        compute='_compute_per_day_cost',
        store=True,
    )
    start_date = fields.Date(string='Start Date')
    end_date = fields.Date(string='End Date')

    @api.depends('assigned_user_ids')
    def _compute_subscription_count(self):
        for rec in self:
            rec.subscription_count = len(rec.assigned_user_ids)

    @api.depends('cost_per_subscription', 'subscription_count')
    def _compute_final_amount(self):
        for rec in self:
            rec.final_amount = (
                (rec.cost_per_subscription or 0.0)
                * (rec.subscription_count or 0)
            )

    @api.depends('final_amount')
    def _compute_per_day_cost(self):
        for rec in self:
            rec.per_day_cost = (rec.final_amount or 0.0) / 30.0
