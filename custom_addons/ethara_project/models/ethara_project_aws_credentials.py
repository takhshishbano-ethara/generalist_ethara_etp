from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class EtharaProjectAwsCredentials(models.Model):
    _name = 'ethara.project.aws.credentials'
    _description = 'Ethara Project AWS Credentials'
    _rec_name = 'access_key_id'

    access_key_id = fields.Char(string='AWS Access Key ID')
    secret_key = fields.Char(
        string='AWS Secret Access Key',
        groups='base.group_system',
    )
    secret_key_is_set = fields.Boolean(
        string='Secret Stored',
        compute='_compute_secret_key_is_set',
        store=False,
    )
    region_name = fields.Char(string='AWS Region', default='us-east-1')

    @api.depends('secret_key')
    def _compute_secret_key_is_set(self):
        for rec in self:
            rec.secret_key_is_set = bool(rec.secret_key)

    @api.model_create_multi
    def create(self, vals_list):
        if self.sudo().search_count([]) and vals_list:
            raise ValidationError(_(
                'Only one AWS credentials record is allowed. '
                'Update the existing one from Ethara Projects -> AWS -> Credentials.'
            ))
        return super().create(vals_list)

    def unlink(self):
        raise ValidationError(_('AWS credentials record cannot be deleted.'))

    @api.model
    def get_singleton(self):
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = super().create([{}])
        return rec

    @api.model
    def get_credentials(self):
        rec = self.sudo().get_singleton()
        return {
            'aws_access_key_id': rec.access_key_id or '',
            'aws_secret_access_key': rec.secret_key or '',
            'region_name': rec.region_name or 'us-east-1',
        }
