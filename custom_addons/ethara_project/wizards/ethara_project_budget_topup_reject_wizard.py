from odoo import fields, models


class EtharaProjectBudgetTopupRejectWizard(models.TransientModel):
    _name = 'ethara.project.budget.topup.reject.wizard'
    _description = 'Reject Ethara Project Budget Top-up Wizard'

    topup_id = fields.Many2one(
        comodel_name='ethara.project.budget.topup',
        string='Top-up',
        required=True,
    )
    reason = fields.Text(string='Rejection Reason', required=True)

    def action_confirm(self):
        self.ensure_one()
        self.topup_id._do_reject(self.reason)
        return {'type': 'ir.actions.act_window_close'}
