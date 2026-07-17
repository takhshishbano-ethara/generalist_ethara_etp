from odoo import _, fields, models
from odoo.exceptions import UserError


class EtharaProjectPhaseRequestReviewWizard(models.TransientModel):
    _name = 'ethara.project.phase.request.review.wizard'
    _description = 'Review Ethara Phase Budget Request Wizard'

    request_id = fields.Many2one(
        comodel_name='ethara.project.phase.request',
        string='Request',
        required=True,
    )
    mode = fields.Selection(
        selection=[
            ('cto_reject', 'CTO Send Back for Changes'),
            ('cfo_request_changes', 'CFO Request Changes'),
        ],
        string='Mode',
        required=True,
        readonly=True,
    )
    note = fields.Text(string='Note', required=True)

    def action_confirm(self):
        self.ensure_one()
        if self.mode == 'cto_reject':
            self.request_id._do_cto_reject(self.note)
        elif self.mode == 'cfo_request_changes':
            self.request_id._do_cfo_request_changes(self.note)
        else:
            raise UserError(_('Unknown review mode.'))
        return {'type': 'ir.actions.act_window_close'}
