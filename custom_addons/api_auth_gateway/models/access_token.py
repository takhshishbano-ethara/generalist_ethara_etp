import uuid
import secrets
from datetime import datetime, timedelta
from odoo import models, fields, api, tools, _
import os
import hashlib
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT
from odoo.http import request

class DepartmentRole(models.Model):
    _name = 'department.role'
    _description = 'API User Role'

    name = fields.Char(required=True)
    url_key = fields.Char(index=True)

class ApiRole(models.Model):
    _name = 'api.role'
    _description = 'API User Role'

    name = fields.Char(required=True)
    department_id = fields.Many2one('department.role', string='Role Department')
    line_ids = fields.Many2many('api.role.line', string="Permissions")
    endpoint_ids = fields.One2many('api.role.endpoint', 'role_id', string='Allowed Endpoints')
    project_type = fields.Selection([('non-stem', 'Non Stem'), ('stem', 'Stem'), ('technical', 'Technical')], default='technical')
    user_type = fields.Char(string='User Type')


HTTP_METHODS = [
    ('GET', 'GET'),
    ('POST', 'POST'),
    ('PUT', 'PUT'),
    ('PATCH', 'PATCH'),
    ('DELETE', 'DELETE'),
    ('OPTIONS', 'OPTIONS'),
    ('HEAD', 'HEAD'),
]


class ApiEndpoint(models.Model):
    _name = 'api.endpoint'
    _description = 'API Endpoint Grant on a Role'
    _rec_name = 'url_pattern'

    url_pattern = fields.Char(string='URL Pattern')
    note = fields.Char()
    domain = fields.Char()

class ApiRoleEndpoint(models.Model):
    _name = 'api.role.endpoint'
    _description = 'API Endpoint Grant on a Role'
    _order = 'url_pattern, method'

    role_id = fields.Many2one('api.role', required=True, ondelete='cascade', index=True)
    api_end_point_id = fields.Many2one('api.endpoint', string='API Endpoint')
    url_pattern = fields.Char(related='api_end_point_id.url_pattern')
    method = fields.Selection(HTTP_METHODS, string='HTTP Method', required=True, default='GET', index=True)
    note = fields.Char()

    _sql_constraints = [
        (
            'role_url_method_uniq',
            'unique(role_id, url_pattern, method)',
            'This endpoint is already granted to the role.',
        ),
    ]

    # @api.onchange('user_type', 'name')
    # def onchange_user_type(self):
    #     user_type = ''
    #     if self.name and self.user_type:
    #         user_type = f"{self.user_type}-({self.name})"
    #     elif self.name:
    #         user_type = self.name
    #     elif self.user_type:
    #         user_type = self.user_type
    #     self.user_type = user_type


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
        expires = fields.Datetime.now() + timedelta(seconds=360000)
        vals = {
            'expiry': expires,
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
            expires = fields.Datetime.now() + timedelta(seconds=360000)
            vals = {
                'user_id': user_id,
                'expiry': expires,
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
        if not self.expiry:
            return True
        return fields.Datetime.now() > self.expiry

    # def has_expired(self):
    #     self.ensure_one()
    #     if not self.expiry:
    #         return True
    #     return fields.Datetime.now() > self.expiry  # Changed from self.expires

class Users(models.Model):
    _inherit = 'res.users'

    token_ids = fields.One2many('api.access_token', 'user_id', string="Access Tokens")
    user_role = fields.Many2one('api.role', string='User Role')
    is_organization_account = fields.Boolean(default=False)
    def write(self, vals):
        res = super(Users, self).write(vals)
        if 'password' in vals or "active" in vals:
            if vals.get('password') or not vals.get('active'):
                for tnk in self.token_ids:
                    tnk.sudo().unlink()
        return res


class ResPartner(models.Model):
    _inherit = "res.partner"

    profile_url = fields.Char(string='Profile URL')
    bio_data = fields.Char(string='Bio Data')
    location = fields.Char(string='Location')
    in_app_notification = fields.Boolean(default=False)
    email_notification = fields.Boolean(default=False)
    push_notification = fields.Boolean(default=False)
    is_organization_account = fields.Boolean(default=False)
