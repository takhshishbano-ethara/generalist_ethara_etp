from odoo import fields, models


class EtharaProjectInfraType(models.Model):
    _name = 'ethara.project.infra.type'
    _description = 'Ethara Infrastructure Type'
    _order = 'sequence, name'

    name = fields.Char(string='Name', required=True)
    code = fields.Char(string='Code')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    provider_id = fields.Many2one(
        'ethara.project.infra.provider',
        string='Provider',
        index=True,
        ondelete='set null',
        help='The cloud provider this service belongs to (AWS, Microsoft, ...).',
    )

    is_aws_managed = fields.Boolean(string='AWS-Managed', default=False, index=True)
    aws_service_code = fields.Char(string='AWS Service Code', index=True)
    primary_attr = fields.Char(string='Primary Attribute')
    attribute_names = fields.Text(string='Attribute Names (JSON)')
    sync_enabled = fields.Boolean(string='Sync Enabled', default=True)
    last_synced_at = fields.Datetime(string='Last Synced At')
    item_count = fields.Integer(string='Cached Items', default=0)

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'This infrastructure type already exists.'),
    ]
