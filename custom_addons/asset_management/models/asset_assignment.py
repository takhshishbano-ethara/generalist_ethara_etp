import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class AssetAssignment(models.Model):
    _name = 'asset.assignment'
    _description = 'Asset Assignment'
    _inherit = ['mail.thread']
    _order = 'assigned_date desc, id desc'

    name = fields.Char(
        string='Reference', required=True, readonly=True, copy=False,
        default='New',
    )
    asset_id = fields.Many2one(
        'asset.asset', string='Asset',
        required=True, ondelete='cascade', index=True, tracking=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        required=True, index=True, tracking=True,
    )
    project_id = fields.Many2one('project.project', string='Project', tracking=True)
    request_id = fields.Many2one(
        'asset.request', string='Source Request', ondelete='set null',
    )

    assigned_date = fields.Date(
        string='Assigned On', default=fields.Date.context_today, required=True,
    )
    expected_return_date = fields.Date(string='Expected Return')
    actual_return_date = fields.Date(string='Returned On', tracking=True)

    duration_days = fields.Integer(
        string='Duration (days)',
        compute='_compute_duration_days', store=True,
    )
    is_active = fields.Boolean(
        string='Currently Active',
        compute='_compute_is_active', store=True, index=True,
    )
    is_overdue = fields.Boolean(
        string='Overdue',
        compute='_compute_is_overdue', store=False,
    )

    condition_in = fields.Selection(
        [
            ('new', 'New'),
            ('good', 'Good'),
            ('fair', 'Fair'),
            ('damaged', 'Damaged'),
        ],
        string='Condition Out (at handover)',
    )
    condition_out = fields.Selection(
        [
            ('new', 'New'),
            ('good', 'Good'),
            ('fair', 'Fair'),
            ('damaged', 'Damaged'),
        ],
        string='Condition In (on return)',
    )

    assigned_by = fields.Many2one(
        'res.users', string='Assigned By',
        default=lambda self: self.env.user, readonly=True,
    )
    notes = fields.Text(string='Notes')

    # ---- Computes ------------------------------------------------------

    @api.depends('assigned_date', 'actual_return_date')
    def _compute_duration_days(self):
        today = fields.Date.today()
        for rec in self:
            if not rec.assigned_date:
                rec.duration_days = 0
                continue
            end = rec.actual_return_date or today
            rec.duration_days = (end - rec.assigned_date).days

    @api.depends('actual_return_date')
    def _compute_is_active(self):
        for rec in self:
            rec.is_active = not rec.actual_return_date

    @api.depends('is_active', 'expected_return_date')
    def _compute_is_overdue(self):
        today = fields.Date.today()
        for rec in self:
            rec.is_overdue = bool(
                rec.is_active and rec.expected_return_date and rec.expected_return_date < today
            )

    # ---- CRUD ----------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('asset.assignment') or 'New'
        records = super().create(vals_list)
        # Enforce single active assignment per asset
        for rec in records:
            if not rec.actual_return_date:
                other = self.search([
                    ('id', '!=', rec.id),
                    ('asset_id', '=', rec.asset_id.id),
                    ('actual_return_date', '=', False),
                ], limit=1)
                if other:
                    raise ValidationError(_(
                        'Asset %s already has an open assignment (%s). Return it before reassigning.'
                    ) % (rec.asset_id.display_name, other.name))
        return records

    # ---- Workflow ------------------------------------------------------

    def action_return(self, condition_out=None):
        for rec in self:
            if rec.actual_return_date:
                raise UserError(_('Assignment %s is already returned.') % rec.name)
            vals = {'actual_return_date': fields.Date.today()}
            if condition_out:
                vals['condition_out'] = condition_out
            rec.write(vals)
            if rec.asset_id:
                rec.asset_id.sudo().write({'state': 'available'})
                rec.asset_id.message_post(
                    body=_('Returned by %s after %s day(s).') % (
                        rec.employee_id.name or '', rec.duration_days,
                    ),
                    subtype_xmlid='mail.mt_note',
                )
            rec.message_post(
                body=_('Assignment closed on %s.') % rec.actual_return_date,
                subtype_xmlid='mail.mt_note',
            )
        return True

    # ---- Crons ---------------------------------------------------------

    @api.model
    def _cron_overdue_assignments(self):
        today = fields.Date.today()
        overdue = self.sudo().search([
            ('actual_return_date', '=', False),
            ('expected_return_date', '!=', False),
            ('expected_return_date', '<', today),
        ])
        manager_group = self.env.ref(
            'asset_management.group_asset_manager', raise_if_not_found=False,
        )
        manager_partner_ids = manager_group.users.partner_id.ids if manager_group else []
        for rec in overdue:
            try:
                partner_ids = list(manager_partner_ids)
                if rec.assigned_by and rec.assigned_by.partner_id:
                    partner_ids.append(rec.assigned_by.partner_id.id)
                rec.message_post(
                    body=_('Overdue: expected return was %s.') % rec.expected_return_date,
                    partner_ids=list(set(partner_ids)),
                    subtype_xmlid='mail.mt_note',
                )
            except Exception as e:  # noqa: BLE001
                _logger.warning('Overdue alert failed for %s: %s', rec.name, e)
