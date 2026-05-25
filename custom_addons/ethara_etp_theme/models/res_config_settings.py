# -*- coding: utf-8 -*-
import base64

from odoo import api, fields, models
from odoo.tools.mimetypes import guess_mimetype


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ethara_theme_mode = fields.Selection(
        selection=[
            ("light", "Light"),
            ("dark", "Dark"),
        ],
        string="Theme Mode",
        config_parameter="ethara_etp_theme.theme_mode",
        default="light",
    )

    ethara_primary_color = fields.Char(
        string="Primary Color",
        config_parameter="ethara_etp_theme.primary_color",
        default="#6366f1",
    )
    ethara_primary_dark_color = fields.Char(
        string="Primary - Hover / Active",
        config_parameter="ethara_etp_theme.primary_dark_color",
        default="#4f46e5",
    )
    ethara_primary_soft_color = fields.Char(
        string="Primary - Soft Highlight",
        config_parameter="ethara_etp_theme.primary_soft_color",
        default="#eef2ff",
    )
    ethara_sidebar_bg = fields.Char(
        string="Sidebar & Card Background",
        config_parameter="ethara_etp_theme.sidebar_bg",
        default="#ffffff",
    )
    ethara_content_bg = fields.Char(
        string="Content Background",
        config_parameter="ethara_etp_theme.content_bg",
        default="#f6f7fb",
    )

    ethara_font = fields.Selection(
        selection=[
            ("inter", "Inter"),
            ("roboto", "Roboto"),
            ("poppins", "Poppins"),
            ("lato", "Lato"),
            ("system", "System Default"),
        ],
        string="Font Family",
        config_parameter="ethara_etp_theme.font",
        default="inter",
    )
    ethara_transition = fields.Selection(
        selection=[
            ("none", "Off"),
            ("fast", "Fast"),
            ("normal", "Normal"),
            ("slow", "Slow"),
        ],
        string="Transition Speed",
        config_parameter="ethara_etp_theme.transition",
        default="normal",
    )

    ethara_border_radius = fields.Integer(
        string="Border Radius (px)",
        config_parameter="ethara_etp_theme.border_radius",
        default=10,
    )
    ethara_density = fields.Selection(
        selection=[
            ("comfortable", "Comfortable"),
            ("compact", "Compact"),
        ],
        string="Density",
        config_parameter="ethara_etp_theme.density",
        default="comfortable",
    )
    ethara_card_shadow = fields.Selection(
        selection=[
            ("none", "None"),
            ("subtle", "Subtle"),
            ("elevated", "Elevated"),
        ],
        string="Card Shadow",
        config_parameter="ethara_etp_theme.card_shadow",
        default="subtle",
    )
    ethara_input_style = fields.Selection(
        selection=[
            ("outlined", "Outlined"),
            ("filled", "Filled"),
            ("underline", "Underline"),
            ("soft", "Soft"),
        ],
        string="Input Style",
        config_parameter="ethara_etp_theme.input_style",
        default="soft",
    )

    ethara_sidebar_width = fields.Integer(
        string="Sidebar Width (px)",
        config_parameter="ethara_etp_theme.sidebar_width",
        default=240,
    )
    ethara_navbar_height = fields.Integer(
        string="Navbar Height (px)",
        config_parameter="ethara_etp_theme.navbar_height",
        default=48,
    )
    ethara_app_icons = fields.Selection(
        selection=[
            ("shown", "Shown"),
            ("hidden", "Hidden"),
        ],
        string="App Icons",
        config_parameter="ethara_etp_theme.app_icons",
        default="shown",
    )
    ethara_sidebar_default = fields.Selection(
        selection=[
            ("expanded", "Expanded"),
            ("collapsed", "Collapsed"),
        ],
        string="Sidebar Default State",
        config_parameter="ethara_etp_theme.sidebar_default",
        default="expanded",
    )
    ethara_sidebar_accent = fields.Selection(
        selection=[
            ("accent", "Colored"),
            ("plain", "Plain"),
        ],
        string="Active Item Style",
        config_parameter="ethara_etp_theme.sidebar_accent",
        default="accent",
    )

    ethara_login_template = fields.Selection(
        selection=[
            ("centered", "Centered Card"),
            ("split", "Split Screen"),
            ("fullscreen", "Full-Screen Background"),
            ("minimal", "Minimal"),
        ],
        string="Login Template",
        config_parameter="ethara_etp_theme.login_template",
        default="centered",
    )
    ethara_login_image = fields.Binary(string="Login Image")

    ethara_chatter_position = fields.Selection(
        selection=[
            ("sided", "Side"),
            ("bottom", "Bottom"),
        ],
        string="Chatter Position",
        config_parameter="ethara_etp_theme.chatter_position",
        default="sided",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        params = self.env["ir.config_parameter"].sudo()
        attachment_id = params.get_param("ethara_etp_theme.login_image_attachment_id")
        image = False
        if attachment_id and str(attachment_id).isdigit():
            attachment = self.env["ir.attachment"].sudo().browse(int(attachment_id))
            if attachment.exists():
                image = attachment.datas
        res["ethara_login_image"] = image
        return res

    def set_values(self):
        super().set_values()
        params = self.env["ir.config_parameter"].sudo()
        attachments = self.env["ir.attachment"].sudo()
        attachment_id = params.get_param("ethara_etp_theme.login_image_attachment_id")
        attachment = (
            attachments.browse(int(attachment_id))
            if attachment_id and str(attachment_id).isdigit()
            else attachments
        )
        attachment = attachment.exists()
        if self.ethara_login_image:
            mimetype = guess_mimetype(base64.b64decode(self.ethara_login_image))
            values = {"datas": self.ethara_login_image, "mimetype": mimetype}
            if attachment:
                attachment.write(values)
            else:
                attachment = attachments.create(
                    dict(values, name="ethara_login_image", type="binary", public=True)
                )
            params.set_param("ethara_etp_theme.login_image_attachment_id", attachment.id)
            params.set_param(
                "ethara_etp_theme.login_image_url", "/web/image/%s" % attachment.id
            )
        else:
            if attachment:
                attachment.unlink()
            params.set_param("ethara_etp_theme.login_image_attachment_id", "")
            params.set_param("ethara_etp_theme.login_image_url", "")
