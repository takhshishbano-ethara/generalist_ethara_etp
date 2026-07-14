# -*- coding: utf-8 -*-
from odoo import models, api


class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.depends('name', 'email', 'login')
    @api.depends_context('kensei2_show_email')
    def _compute_display_name(self):
        """Show the email address (not the name) when a field explicitly asks
        for it via ``context={'kensei2_show_email': True}``.

        Odoo's ``res.users`` display is just the name; only ``res.partner``
        honours the stock ``show_email`` key. The Kensei2 Team-Management "Email"
        dropdown needs to read by email, so we opt in with our own context flag
        and leave the display unchanged everywhere else.
        """
        super()._compute_display_name()
        if not self.env.context.get('kensei2_show_email'):
            return
        for user in self:
            label = user.email or user.login
            if label:
                user.display_name = label
