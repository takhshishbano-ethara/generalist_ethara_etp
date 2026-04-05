# -*- coding: utf-8 -*-
from odoo import fields, models


class KaijuApp(models.Model):
    _name = "kaiju.app"
    _description = "Kaiju Registered Application"
    _order = "name"

    name = fields.Char(string="App Name", required=True, index=True)
    description = fields.Text(string="Description")
    repo_url = fields.Char(string="Default Repository URL")
    default_branch = fields.Char(string="Default Branch", default="main")
    default_dockerfile = fields.Char(
        string="Default Dockerfile Path", default="Dockerfile"
    )
    active = fields.Boolean(string="Active", default=True)

    _sql_constraints = [
        ("name_unique", "UNIQUE(name)", "App name must be unique."),
    ]
