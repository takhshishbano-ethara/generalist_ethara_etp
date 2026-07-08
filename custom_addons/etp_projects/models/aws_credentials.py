from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .encryption_helper import decrypt_value, encrypt_value


class EtpAwsCredentials(models.Model):
    _name = "etp.aws.credentials"
    _description = "ETP AWS Credentials"
    _rec_name = "access_key_id"

    is_enabled = fields.Boolean(
        string="Enable AWS Integration",
        default=False,
    )
    access_key_id = fields.Char(string="AWS Access Key ID")
    secret_key_encrypted = fields.Char(
        string="Encrypted Secret",
        groups="base.group_system",
    )
    secret_key = fields.Char(
        string="AWS Secret Access Key",
        compute="_compute_secret_key",
        inverse="_inverse_secret_key",
        store=False,
    )
    secret_key_is_set = fields.Boolean(
        string="Secret Stored",
        compute="_compute_secret_key_is_set",
        store=False,
    )
    region_name = fields.Char(string="AWS Region", default="us-east-1")

    @api.depends("secret_key_encrypted")
    def _compute_secret_key(self):
        for rec in self:
            rec.secret_key = decrypt_value(rec.env, rec.secret_key_encrypted or "")

    @api.depends("secret_key_encrypted")
    def _compute_secret_key_is_set(self):
        for rec in self:
            rec.secret_key_is_set = bool(rec.secret_key_encrypted)

    def _inverse_secret_key(self):
        for rec in self:
            rec.secret_key_encrypted = encrypt_value(rec.env, rec.secret_key or "")

    @api.model_create_multi
    def create(self, vals_list):
        if self.sudo().search_count([]) and vals_list:
            raise ValidationError(_(
                "Only one AWS credentials record is allowed. "
                "Update the existing one via Configuration → Settings."
            ))
        return super().create(vals_list)

    def unlink(self):
        raise ValidationError(_(
            "AWS credentials record cannot be deleted. "
            "Disable the integration instead."
        ))

    @api.model
    def get_singleton(self):
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = super().create([{}])
        return rec

    @api.model
    def get_credentials(self):
        rec = self.sudo().get_singleton()
        if not rec.is_enabled:
            return {}
        return {
            "aws_access_key_id": rec.access_key_id or "",
            "aws_secret_access_key": decrypt_value(rec.env, rec.secret_key_encrypted or ""),
            "region_name": rec.region_name or "us-east-1",
        }
