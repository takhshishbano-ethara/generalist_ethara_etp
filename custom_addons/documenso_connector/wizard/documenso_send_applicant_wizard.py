# -*- coding: utf-8 -*-
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class DocumensoSendApplicantWizard(models.TransientModel):
    _name = 'documenso.send.applicant.wizard'
    _description = 'Send Documenso Document to Applicants'

    job_id = fields.Many2one('hr.job', string='Job', required=True, ondelete='cascade')
    applicant_ids = fields.Many2many(
        'hr.applicant',
        'documenso_send_applicant_wizard_applicant_rel',
        'wizard_id', 'applicant_id',
        string='Applicants', required=True,
        default=lambda self: self._default_applicant_ids(),
    )
    template_ids = fields.Many2many(
        'documenso.template',
        'documenso_send_applicant_wizard_template_rel',
        'wizard_id', 'template_id',
        string='Templates', required=True,
        domain="[('active', '=', True), ('is_published', '=', True), ('doc_class', 'in', ['contract', 'compliance']), '|', ('job_id', '=', job_id), ('job_id', '=', False)]",
    )
    title_override = fields.Char(string='Title Override')
    distribute = fields.Boolean(string='Send Emails Automatically', default=True)
    extra_prefill_json = fields.Text(
        string='Extra Prefill (JSON)',
        help='Optional JSON object with additional prefill values (keys are field labels).')
    skipped_applicant_ids = fields.Many2many(
        'hr.applicant',
        'documenso_send_applicant_wizard_skipped_rel',
        'wizard_id', 'applicant_id',
        string='Skipped Applicants',
        compute='_compute_skipped_applicant_ids',
    )

    @api.model
    def _default_applicant_ids(self):
        ctx_ids = self.env.context.get('default_applicant_ids')
        if ctx_ids:
            return ctx_ids
        active_ids = self.env.context.get('active_ids')
        active_model = self.env.context.get('active_model')
        if active_ids and active_model == 'hr.applicant':
            return [(6, 0, active_ids)]
        return False

    @api.depends('applicant_ids')
    def _compute_skipped_applicant_ids(self):
        for wizard in self:
            wizard.skipped_applicant_ids = wizard.applicant_ids.filtered(
                lambda a: not a.email_from)

    def action_send(self):
        self.ensure_one()
        if not self.template_ids:
            raise UserError(_("Please select at least one template to send."))

        extra_prefill = {}
        if self.extra_prefill_json:
            try:
                parsed = json.loads(self.extra_prefill_json)
                if not isinstance(parsed, dict):
                    raise ValueError()
                extra_prefill = parsed
            except (ValueError, TypeError):
                raise UserError(_("Extra Prefill must be a valid JSON object."))

        sendable = self.applicant_ids.filtered(lambda a: a.email_from)
        skipped = self.applicant_ids - sendable

        if not sendable:
            raise UserError(_(
                "None of the selected applicants have an email address. Nothing to send."))

        Contract = self.env['documenso.contract']
        created_contracts = Contract.browse()
        failures = []

        for applicant in sendable:
            first_doc_class = self.template_ids[:1].doc_class or 'contract'
            contract = Contract.create({
                'applicant_id': applicant.id,
                'job_id': self.job_id.id,
                'doc_class': first_doc_class,
                'template_ids': [(6, 0, self.template_ids.ids)],
                'template_id': self.template_ids[:1].id,
            })
            try:
                contract.action_send(
                    template_ids=self.template_ids.ids,
                    distribute=self.distribute,
                    title_override=self.title_override or False,
                    extra_prefill=extra_prefill,
                )
                created_contracts |= contract
            except Exception as exc:
                failures.append((applicant, str(exc)))

        if skipped or failures:
            lines = []
            if skipped:
                lines.append(_("Skipped (no email):"))
                lines.extend('  - %s' % (a.partner_name or a.display_name) for a in skipped)
            if failures:
                lines.append(_("Failed to send:"))
                lines.extend('  - %s: %s' % (
                    a.partner_name or a.display_name, err) for a, err in failures)
            if not created_contracts:
                raise UserError('\n'.join(lines))
            for contract in created_contracts:
                contract.message_post(body='\n'.join(lines))

        if len(created_contracts) == 1:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Documenso Document'),
                'res_model': 'documenso.contract',
                'res_id': created_contracts.id,
                'view_mode': 'form',
                'target': 'current',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _('Documenso Documents'),
            'res_model': 'documenso.contract',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_contracts.ids)],
            'target': 'current',
        }
