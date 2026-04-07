from odoo import models, fields


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    is_paid = fields.Boolean(
        string='Is Paid',
        default=False,
        help='Set to True when leave is approved, False when rejected',
    )

    def action_approve(self):
        res = super().action_approve()
        self.write({'is_paid': True})
        return res

    def action_refuse(self):
        res = super().action_refuse()
        self.write({'is_paid': False})
        return res
