import uuid
import secrets
from datetime import datetime, timedelta
from odoo import models, fields, api, tools, _
import os
import hashlib
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT
from odoo.http import request

class ApiRole(models.Model):
    _name = 'api.role'
    _description = 'API User Role'

    name = fields.Char(required=True)
    line_ids = fields.Many2many('api.role.line', string="Permissions")
    project_type = fields.Selection([('non-stem', 'Non Stem'), ('stem', 'Stem'), ('technical', 'Technical')], default='technical')
    user_type = fields.Char(string='User Type')

    @api.onchange('user_type', 'name')
    def onchange_user_type(self):
        user_type = ''
        if self.name and self.user_type:
            user_type = f"{self.user_type}-({self.name})"
        elif self.name:
            user_type = self.name
        elif self.user_type:
            user_type = self.user_type
        self.user_type = user_type


class ApiRoleLine(models.Model):
    _name = 'api.role.line'
    _rec_name = 'menu_name'

    menu_name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    can_read = fields.Boolean(default=True)
    can_write = fields.Boolean(default=False)
    can_create = fields.Boolean(default=False)
    can_delete = fields.Boolean(default=False)
    parent_id = fields.Many2one('api.role.line')


def nonce(length=40, prefix='access_token'):
    rbytes = os.urandom(length)
    return '{}_{}'.format(prefix, str(hashlib.sha1(rbytes).hexdigest()))


class APIAccessToken(models.Model):
    _name = 'api.access_token'
    _description = "Access Token"

    user_id = fields.Many2one('res.users', required=True, ondelete='cascade')
    access_token = fields.Char(index=True)
    refresh_token = fields.Char(index=True)
    expiry = fields.Datetime()
    browser_name = fields.Char(string='Browser Name')
    os_name = fields.Char(string='OS Name')
    location = fields.Char(string='Location')
    theme = fields.Selection([('light', 'Light'), ('dark', 'Dark'), ('system', 'System')], default='light')
    table_density = fields.Selection([('compact', 'Compact'), ('default', 'Default'), ('comfortable', 'Comfortable')], default='compact')
    collapse_sidebar = fields.Boolean(default=False)

    def update_access_token(self):
        expires = datetime.now() + timedelta(seconds=3600)
        vals = {
            'expiry': expires.strftime(DEFAULT_SERVER_DATETIME_FORMAT),
            'access_token': nonce()
        }
        self.sudo().write(vals)
        return self.sudo().access_token, self.sudo().refresh_token

    def find_one_or_create_token(self, user_id=None, create=False):
        if not user_id:
            user_id = request.env.user.id

        access_token = self.env['api.access_token'].sudo().search([('user_id', '=', user_id)], order='id DESC', limit=1)
        if access_token:
            access_token = access_token[0]
            if access_token.has_expired():
                access_token = None
        if create:
            expires = datetime.now() + timedelta(seconds=3600)
            vals = {
                'user_id': user_id,
                'expiry': expires.strftime(DEFAULT_SERVER_DATETIME_FORMAT),
                'access_token': nonce(),
                'refresh_token': nonce(),
            }
            access_token = self.env['api.access_token'].sudo().create(vals)
            self._cr.commit()
        if not access_token:
            return None
        return access_token.access_token, access_token.refresh_token

    def has_expired(self):
        self.ensure_one()
        return datetime.now() > fields.Datetime.from_string(self.expiry)

    # def has_expired(self):
    #     self.ensure_one()
    #     if not self.expiry:
    #         return True
    #     return fields.Datetime.now() > self.expiry  # Changed from self.expires

class Users(models.Model):
    _inherit = 'res.users'

    token_ids = fields.One2many('api.access_token', 'user_id', string="Access Tokens")
    user_role = fields.Many2one('api.role', string='User Role')

    ROLE_GROUP_MAP = {
        'CTO': 'etp_user_roles.group_cto',
        'PL': 'etp_user_roles.group_project_lead',
        'PL-Stem': 'etp_user_roles.group_project_lead',
        'PL-Non-Stem': 'etp_user_roles.group_project_lead',
        'QC': 'etp_user_roles.group_quality_reviewer',
        'QC-Stem': 'etp_user_roles.group_quality_reviewer',
        'QC-Non-Stem': 'etp_user_roles.group_quality_reviewer',
        'SWE': 'etp_user_roles.group_tasker',
        'AIRE': 'etp_user_roles.group_tasker',
        'Tasker': 'etp_user_roles.group_tasker',
        'Tasker-Stem': 'etp_user_roles.group_tasker',
        'Tasker-Non-Stem': 'etp_user_roles.group_tasker',
    }

    def _sync_role_groups(self):
        managed_groups = self.env['res.groups']
        for xmlid in set(self.ROLE_GROUP_MAP.values()):
            grp = self.env.ref(xmlid, raise_if_not_found=False)
            if grp:
                managed_groups |= grp
        managed_ids = set(managed_groups.ids)

        for user in self:
            new_group = None
            if user.user_role and user.user_role.user_type:
                xmlid = self.ROLE_GROUP_MAP.get(user.user_role.user_type)
                if xmlid:
                    new_group = self.env.ref(xmlid, raise_if_not_found=False)

            commands = []
            current_managed = managed_ids & set(user.groups_id.ids)
            for gid in current_managed:
                commands.append((3, gid))
            if new_group:
                commands.append((4, new_group.id))

            if commands:
                super(Users, user).write({'groups_id': commands})

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users_with_role = users.filtered(lambda u: u.user_role)
        if users_with_role:
            users_with_role.sudo()._sync_role_groups()
        return users

    def write(self, vals):
        res = super(Users, self).write(vals)
        if 'password' in vals or "active" in vals:
            if vals.get('password') or not vals.get('active'):
                for tnk in self.token_ids:
                    tnk.sudo().unlink()
        if 'user_role' in vals:
            self.sudo()._sync_role_groups()
        return res


class ResPartner(models.Model):
    _inherit = "res.partner"

    profile_url = fields.Char(string='Profile URL')
    bio_data = fields.Char(string='Bio Data')
    location = fields.Char(string='Location')
    in_app_notification = fields.Boolean(default=False)
    email_notification = fields.Boolean(default=False)
    push_notification = fields.Boolean(default=False)
