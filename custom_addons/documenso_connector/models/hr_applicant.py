# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class HrApplicant(models.Model):
    _inherit = 'hr.applicant'

    documenso_contract_ids = fields.One2many(
        'documenso.contract', 'applicant_id', string='Documenso Documents')
    documenso_contract_count = fields.Integer(
        string='Documenso Documents', compute='_compute_documenso_contract_count')
    documenso_last_status = fields.Selection(
        [
            ('DRAFT', 'Draft'),
            ('SENT', 'Sent'),
            ('OPENED', 'Opened'),
            ('SIGNED', 'Signed'),
            ('REJECTED', 'Rejected'),
            ('CANCELLED', 'Cancelled'),
            ('EXPIRED', 'Expired'),
        ],
        string='Last Documenso Status',
        compute='_compute_documenso_last_status',
    )

    @api.depends('documenso_contract_ids')
    def _compute_documenso_contract_count(self):
        for applicant in self:
            applicant.documenso_contract_count = len(applicant.documenso_contract_ids)

    @api.depends('documenso_contract_ids.status',
                 'documenso_contract_ids.sent_at',
                 'documenso_contract_ids.create_date')
    def _compute_documenso_last_status(self):
        for applicant in self:
            contracts = applicant.documenso_contract_ids.sorted(
                key=lambda c: (c.sent_at or c.create_date), reverse=True)
            applicant.documenso_last_status = contracts[:1].status if contracts else False

    def action_documenso_send_document(self):
        self.ensure_one()
        if not self.email_from:
            raise UserError(_(
                "Applicant %s has no email address; cannot send a Documenso document.")
                % (self.partner_name or self.display_name))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Send Documenso Document'),
            'res_model': 'documenso.send.applicant.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_applicant_ids': [(6, 0, [self.id])],
            },
        }

    def action_documenso_view_documents(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Documenso Documents'),
            'res_model': 'documenso.contract',
            'view_mode': 'list,form',
            'domain': [('applicant_id', '=', self.id)],
            'context': {'default_applicant_id': self.id},
        }
