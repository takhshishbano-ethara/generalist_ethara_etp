# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    documenso_contract_ids = fields.One2many(
        'documenso.contract', 'employee_id', string='Documenso Contracts')
    documenso_contract_count = fields.Integer(
        string='Contracts', compute='_compute_documenso_contract_count')
    documenso_last_status = fields.Selection([
        ('DRAFT', 'Draft'),
        ('SENT', 'Sent'),
        ('OPENED', 'Opened'),
        ('SIGNED', 'Signed'),
        ('REJECTED', 'Rejected'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED', 'Expired'),
    ], string='Last Documenso Status', compute='_compute_documenso_last_status')

    def _compute_documenso_contract_count(self):
        for record in self:
            record.documenso_contract_count = len(record.documenso_contract_ids)

    def _compute_documenso_last_status(self):
        for record in self:
            latest = record.documenso_contract_ids.sorted(lambda c: c.sent_at or c.create_date, reverse=True)[:1]
            record.documenso_last_status = latest.status if latest else False

    def action_documenso_send_contract(self):
        self.ensure_one()
        if not self.work_email:
            raise UserError(_("Employee %s has no work email; add one before sending a contract.") % self.name)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Send Documenso Contract'),
            'res_model': 'documenso.send.contract.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_employee_id': self.id},
        }

    def action_documenso_view_contracts(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Documenso Contracts'),
            'res_model': 'documenso.contract',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
            'context': {'default_employee_id': self.id},
        }
