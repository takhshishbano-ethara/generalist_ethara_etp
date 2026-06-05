from odoo import api, fields, models


class AssetCategory(models.Model):
    _name = 'asset.category'
    _description = 'Asset Category'
    _order = 'name'

    name = fields.Char(string='Name', required=True)
    code = fields.Char(string='Code')
    asset_type = fields.Selection(
        [
            ('hardware', 'Hardware'),
            ('software', 'Software'),
            ('subscription', 'Subscription'),
            ('accessory', 'Accessory'),
        ],
        string='Asset Type', required=True, default='hardware',
    )
    is_returnable = fields.Boolean(string='Returnable', default=True)
    default_duration_days = fields.Integer(
        string='Default Duration (days)',
        help='Suggested assignment duration. 0 = open-ended.',
        default=0,
    )
    description = fields.Text(string='Description')
    active = fields.Boolean(string='Active', default=True)

    asset_ids = fields.One2many('asset.asset', 'category_id', string='Assets')
    asset_count = fields.Integer(
        string='# Assets', compute='_compute_asset_count',
    )

    @api.depends('asset_ids')
    def _compute_asset_count(self):
        for rec in self:
            rec.asset_count = len(rec.asset_ids)

    def action_open_assets(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.name,
            'res_model': 'asset.asset',
            'view_mode': 'list,form',
            'domain': [('category_id', '=', self.id)],
            'context': {'default_category_id': self.id},
        }
