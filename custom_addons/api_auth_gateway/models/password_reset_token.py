import secrets
import hashlib
from datetime import datetime, timedelta
from odoo import models, fields, api
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT


class PasswordResetToken(models.Model):
    _name = 'api.password_reset_token'
    _description = 'Password Reset Token'

    user_id = fields.Many2one('res.users', required=True, ondelete='cascade', index=True)
    token = fields.Char(required=True, index=True)
    expiry = fields.Datetime(required=True)
    used = fields.Boolean(default=False)

    @api.model
    def generate_reset_token(self, user_id):
        """Generate a unique reset token with 1-hour expiry. Invalidate any prior tokens."""
        # Invalidate all existing unused tokens for this user
        self.sudo().search([
            ('user_id', '=', user_id),
            ('used', '=', False),
        ]).write({'used': True})

        raw = secrets.token_urlsafe(48)
        token = hashlib.sha256(raw.encode()).hexdigest()
        expiry = datetime.now() + timedelta(hours=1)

        self.sudo().create({
            'user_id': user_id,
            'token': token,
            'expiry': expiry.strftime(DEFAULT_SERVER_DATETIME_FORMAT),
        })
        return token

    @api.model
    def validate_reset_token(self, token):
        """
        Validate a reset token. Returns the token record if valid, False otherwise.
        Checks: exists, not used, not expired.
        """
        record = self.sudo().search([
            ('token', '=', token),
            ('used', '=', False),
        ], limit=1)

        if not record:
            return False

        if datetime.now() > fields.Datetime.from_string(record.expiry):
            return False

        return record
