from odoo import models


class ResCompany(models.Model):
    _inherit = 'res.company'

    def _get_notification_from_email(self):
        company = self[:1] or self.env.company
        if company.email:
            return company.email
        mail_server = self.env['ir.mail_server'].sudo().search(
            [], order='sequence, id', limit=1,
        )
        return mail_server.smtp_user or ''
