# -*- coding: utf-8 -*-
from odoo import models, fields


class KaijuSettings(models.TransientModel):
    _inherit = "res.config.settings"

    kaiju_argo_server_url = fields.Char(
        string="Argo Server URL",
        config_parameter="kaiju.argo_server_url",
        default="https://argo-workflows-server.argo.svc.cluster.local:2746",
    )
    kaiju_argo_namespace = fields.Char(
        string="Argo Namespace",
        config_parameter="kaiju.argo_namespace",
        default="argo",
    )
    kaiju_argo_token_path = fields.Char(
        string="SA Token Path",
        config_parameter="kaiju.argo_token_path",
        default="/var/run/secrets/kubernetes.io/serviceaccount/token",
    )
    kaiju_argo_verify_tls = fields.Boolean(
        string="Verify TLS",
        config_parameter="kaiju.argo_verify_tls",
    )
    kaiju_odoo_internal_url = fields.Char(
        string="Odoo Internal URL",
        config_parameter="kaiju.odoo_internal_url",
        default="http://odoo-web.odoo.svc:8069",
    )
    kaiju_webhook_token = fields.Char(
        string="Webhook Token",
        config_parameter="kaiju.webhook_token",
    )
