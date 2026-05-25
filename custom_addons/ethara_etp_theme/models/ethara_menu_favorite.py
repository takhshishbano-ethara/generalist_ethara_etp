# -*- coding: utf-8 -*-
from odoo import api, fields, models


class EtharaMenuFavorite(models.Model):
    _name = "ethara.menu.favorite"
    _description = "Ethara Sidebar Favorite Menu"
    _rec_name = "menu_id"

    user_id = fields.Many2one(
        "res.users",
        string="User",
        required=True,
        index=True,
        ondelete="cascade",
        default=lambda self: self.env.uid,
    )
    menu_id = fields.Many2one(
        "ir.ui.menu",
        string="Menu",
        required=True,
        ondelete="cascade",
    )

    _sql_constraints = [
        (
            "user_menu_unique",
            "unique(user_id, menu_id)",
            "This menu is already in your favorites.",
        ),
    ]

    @api.model
    def get_favorite_menu_ids(self):
        return self.search([("user_id", "=", self.env.uid)]).menu_id.ids

    @api.model
    def toggle_favorite(self, menu_id):
        favorite = self.search(
            [("user_id", "=", self.env.uid), ("menu_id", "=", menu_id)],
            limit=1,
        )
        if favorite:
            favorite.unlink()
            return False
        self.create({"user_id": self.env.uid, "menu_id": menu_id})
        return True
