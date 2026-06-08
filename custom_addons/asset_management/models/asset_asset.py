import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class AssetAsset(models.Model):
    _name = 'asset.asset'
    _description = 'Asset'
    _inherit = ['mail.thread']
    _order = 'asset_code desc'

    name = fields.Char(
        string='Display Name',
        compute='_compute_name', store=True,
    )
    asset_code = fields.Char(
        string='Asset Code',
        required=True, readonly=True, copy=False,
        default='New',
        index=True,
    )
    category_id = fields.Many2one(
        'asset.category', string='Category',
        required=True, tracking=True,
    )
    asset_type = fields.Selection(
        related='category_id.asset_type', store=True, readonly=True,
    )
    model = fields.Char(string='Model')
    serial_number = fields.Char(string='Serial Number', tracking=True)
    vendor = fields.Char(string='Vendor')
    purchase_date = fields.Date(string='Purchase Date')
    warranty_expiry = fields.Date(string='Warranty Expiry')
    renewal_date = fields.Date(string='Renewal Date')
    value = fields.Float(string='Value')
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        default=lambda self: self.env.company.currency_id,
    )
    state = fields.Selection(
        [
            ('available', 'Available'),
            ('assigned', 'Assigned'),
            ('maintenance', 'Maintenance'),
            ('retired', 'Retired'),
        ],
        string='Status', default='available', tracking=True, index=True,
    )
    assignment_ids = fields.One2many(
        'asset.assignment', 'asset_id', string='Assignment History',
    )
    current_assignment_id = fields.Many2one(
        'asset.assignment', string='Current Assignment',
        compute='_compute_current_assignment', store=True,
    )
    current_employee_id = fields.Many2one(
        'hr.employee', string='Currently Held By',
        related='current_assignment_id.employee_id', store=True, readonly=True,
    )
    assignment_count = fields.Integer(
        string='# Assignments', compute='_compute_assignment_count',
    )
    notes = fields.Text(string='Notes')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', string='Company',
        default=lambda self: self.env.company,
    )

    # ---- Computes ------------------------------------------------------

    @api.depends('asset_code', 'model', 'category_id.name')
    def _compute_name(self):
        for rec in self:
            label = rec.model or (rec.category_id.name if rec.category_id else _('Asset'))
            rec.name = '%s [%s]' % (label, rec.asset_code or '')

    @api.depends('assignment_ids', 'assignment_ids.actual_return_date')
    def _compute_current_assignment(self):
        for rec in self:
            active = rec.assignment_ids.filtered(lambda a: not a.actual_return_date)
            rec.current_assignment_id = active[:1].id if active else False

    @api.depends('assignment_ids')
    def _compute_assignment_count(self):
        for rec in self:
            rec.assignment_count = len(rec.assignment_ids)

    # ---- Constraints ---------------------------------------------------

    @api.constrains('serial_number', 'state')
    def _check_serial_unique(self):
        for rec in self:
            if not rec.serial_number:
                continue
            if rec.state == 'retired':
                continue
            dup = self.search([
                ('id', '!=', rec.id),
                ('serial_number', '=', rec.serial_number),
                ('state', '!=', 'retired'),
            ], limit=1)
            if dup:
                raise ValidationError(_(
                    'Serial number "%s" is already used on asset %s.'
                ) % (rec.serial_number, dup.asset_code))

    # ---- CRUD ----------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('asset_code') or vals.get('asset_code') == 'New':
                vals['asset_code'] = self.env['ir.sequence'].next_by_code('asset.asset') or 'New'
        return super().create(vals_list)

    # ---- State actions -------------------------------------------------

    def _block_if_assigned(self, target):
        for rec in self:
            if rec.current_assignment_id:
                raise UserError(_(
                    'Cannot move %s to %s while it is held by %s. Return it first.'
                ) % (rec.display_name, target, rec.current_employee_id.name or _('an employee')))

    def action_set_available(self):
        self._block_if_assigned(_('Available'))
        self.write({'state': 'available'})

    def action_set_maintenance(self):
        self._block_if_assigned(_('Maintenance'))
        self.write({'state': 'maintenance'})

    def action_retire(self):
        self._block_if_assigned(_('Retired'))
        self.write({'state': 'retired'})

    def action_open_assignments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Assignment History'),
            'res_model': 'asset.assignment',
            'view_mode': 'list,form',
            'domain': [('asset_id', '=', self.id)],
            'context': {'default_asset_id': self.id},
        }

    # ---- Crons ---------------------------------------------------------

    @api.model
    def _cron_warranty_reminder(self, days_ahead=30):
        today = fields.Date.today()
        soon = today + timedelta(days=days_ahead)
        assets = self.sudo().search([
            ('asset_type', '=', 'hardware'),
            ('warranty_expiry', '!=', False),
            ('warranty_expiry', '>=', today),
            ('warranty_expiry', '<=', soon),
            ('state', 'not in', ('retired',)),
        ])
        for asset in assets:
            try:
                asset.message_post(
                    body=_('Warranty expires on %s.') % asset.warranty_expiry,
                    subtype_xmlid='mail.mt_note',
                )
                _logger.info(
                    'Warranty reminder: asset=%s exp=%s', asset.asset_code, asset.warranty_expiry,
                )
            except Exception as e:  # noqa: BLE001 — cron must not blow up the schedule
                _logger.warning('Warranty reminder failed for %s: %s', asset.asset_code, e)

    @api.model
    def _cron_subscription_renewal(self, days_ahead=14):
        today = fields.Date.today()
        soon = today + timedelta(days=days_ahead)
        assets = self.sudo().search([
            ('asset_type', '=', 'subscription'),
            ('renewal_date', '!=', False),
            ('renewal_date', '>=', today),
            ('renewal_date', '<=', soon),
            ('state', 'not in', ('retired',)),
        ])
        for asset in assets:
            try:
                asset.message_post(
                    body=_('Subscription renewal due on %s.') % asset.renewal_date,
                    subtype_xmlid='mail.mt_note',
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning('Subscription reminder failed for %s: %s', asset.asset_code, e)
