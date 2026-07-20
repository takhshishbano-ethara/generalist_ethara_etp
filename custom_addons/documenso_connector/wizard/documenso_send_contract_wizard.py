import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DocumensoSendContractWizard(models.TransientModel):
    _name = 'documenso.send.contract.wizard'
    _description = 'Send Documenso Contract'

    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True,
        default=lambda self: self.env.context.get('default_employee_id'))
    employee_email = fields.Char(related='employee_id.work_email', readonly=True)
    doc_class = fields.Selection([
        ('contract', 'Contract'),
        ('compliance', 'Compliance'),
        ('offer', 'Offer'),
    ], string='Document Class', default='contract', required=True)
    template_ids = fields.Many2many(
        'documenso.template', string='Templates', required=True,
        domain="[('active', '=', True), ('is_published', '=', True), ('doc_class', '=', doc_class)]")
    title_override = fields.Char(string='Title Override')
    distribute = fields.Boolean(string='Send Immediately', default=True)
    extra_prefill_json = fields.Text(
        string='Extra Prefill (JSON)',
        help='Optional JSON dict of extra field label -> value pairs merged into the employee prefill.')
    existing_contract_id = fields.Many2one(
        'documenso.contract', string='Existing Contract',
        compute='_compute_existing_contract')

    @api.depends('employee_id')
    def _compute_existing_contract(self):
        Contract = self.env['documenso.contract']
        for wizard in self:
            if not wizard.employee_id:
                wizard.existing_contract_id = False
                continue
            wizard.existing_contract_id = Contract.search([
                ('employee_id', '=', wizard.employee_id.id),
                ('status', 'not in', ('SIGNED', 'CANCELLED', 'REJECTED', 'EXPIRED')),
            ], limit=1, order='sent_at desc, id desc')

    def action_send(self):
        self.ensure_one()
        if not self.employee_id.work_email:
            raise UserError(_("Employee %s has no work email set.") % self.employee_id.display_name)
        if not self.template_ids:
            raise UserError(_("Select at least one template."))

        extra = None
        if self.extra_prefill_json:
            try:
                extra = json.loads(self.extra_prefill_json)
            except (TypeError, ValueError) as exc:
                raise UserError(_("Extra Prefill JSON is invalid: %s") % exc)
            if not isinstance(extra, dict):
                raise UserError(_("Extra Prefill JSON must be an object."))

        contract = self.existing_contract_id
        if not contract:
            contract = self.env['documenso.contract'].create({
                'employee_id': self.employee_id.id,
            })

        contract.action_send(
            template_ids=self.template_ids.ids,
            distribute=self.distribute,
            title_override=self.title_override or None,
            extra_prefill=extra,
        )

        return {
            'type': 'ir.actions.act_window',
            'name': _('Documenso Contract'),
            'res_model': 'documenso.contract',
            'res_id': contract.id,
            'view_mode': 'form',
            'target': 'current',
        }
