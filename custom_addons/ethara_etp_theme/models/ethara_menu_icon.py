# -*- coding: utf-8 -*-
from odoo import fields, models


class EtharaMenuIcon(models.Model):
    _name = "ethara.menu.icon"
    _description = "Ethara Sidebar Menu Icon Override"
    _rec_name = "menu_id"
    _order = "menu_id"

    menu_id = fields.Many2one(
        "ir.ui.menu",
        string="Application",
        required=True,
        ondelete="cascade",
        domain="[('parent_id', '=', False)]",
        help="Top-level application whose sidebar icon you want to override.",
    )
    icon_type = fields.Selection(
        selection=[
            ("font", "Font Icon"),
            ("image", "Uploaded Image"),
        ],
        string="Icon Type",
        required=True,
        default="font",
    )
    icon_class = fields.Char(
        string="Font Icon Class",
        default="fa fa-circle",
        help="FontAwesome class, e.g. 'fa fa-rocket'.",
    )
    icon_color = fields.Char(
        string="Glyph Color",
        default="#ffffff",
        help="Color of the FontAwesome glyph.",
    )
    icon_bg_color = fields.Char(
        string="Background Color",
        default="#6366f1",
        help="Background color of the icon tile.",
    )
    icon_image = fields.Binary(
        string="Icon Image",
        help="Square image (PNG or SVG) used as the sidebar icon.",
    )

    _sql_constraints = [
        (
            "menu_id_unique",
            "unique(menu_id)",
            "An icon override already exists for this application.",
        ),
    ]
