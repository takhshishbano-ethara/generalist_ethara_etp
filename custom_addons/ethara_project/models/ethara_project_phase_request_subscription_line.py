from odoo import api, fields, models


class EtharaProjectPhaseRequestSubscriptionLine(models.Model):
    _name = 'ethara.project.phase.request.subscription.line'
    _description = 'Ethara Project Phase Request Subscription Line'
    _order = 'id'

    request_id = fields.Many2one(
        comodel_name='ethara.project.phase.request',
        string='Request',
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
        string='Cost per Subscription (USD)',
        related='subscription_id.cost',
        store=True,
        readonly=True,
    )
    assigned_user_ids = fields.Many2many(
        comodel_name='res.users',
        relation='ethara_project_phase_request_sub_line_user_rel',
        column1='request_sub_line_id',
        column2='user_id',
        string='Assigned To',
    )
    subscription_count = fields.Integer(
        string='No. of Subscriptions',
        compute='_compute_subscription_count',
        store=True,
    )
    final_amount = fields.Float(
        string='Monthly Cost (USD)',
        compute='_compute_final_amount',
        store=True,
    )
    requested_amount = fields.Float(string='Requested (USD)')
    approved_amount = fields.Float(string='Approved (USD)')

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
