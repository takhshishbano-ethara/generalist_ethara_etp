# -*- coding: utf-8 -*-
import re
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SkollPersona(models.Model):
    _name = "skoll.persona"
    _description = "Skoll Persona"
    _order = "name"

    name = fields.Char(
        string="Name",
        required=True,
        index=True,
        help="Unique identifier for this persona (auto-sanitized to lowercase-hyphenated).",
    )
    active = fields.Boolean(default=True)

    soul_md = fields.Text(
        string="Soul (Markdown)",
        help="Core personality and behaviour instructions.",
    )
    memory_md = fields.Text(
        string="Memory (Markdown)",
        help="Persistent memory / context for this persona.",
    )
    agents_md = fields.Text(
        string="Agents (Markdown)",
        help="Agent configuration and routing rules.",
    )

    task_ids = fields.One2many(
        "skoll.skoll",
        "persona_id",
        string="Tasks",
    )
    task_count = fields.Integer(
        string="Tasks",
        compute="_compute_task_count",
    )

    is_skoll_admin = fields.Boolean(
        string="Is Admin",
        compute="_compute_is_skoll_admin",
        search="_search_is_skoll_admin",
    )

    _sql_constraints = [
        ("name_unique", "UNIQUE(name)", "Persona name must be unique."),
    ]

    @api.depends("task_ids")
    def _compute_task_count(self):
        for rec in self:
            rec.task_count = len(rec.task_ids)

    @api.depends_context("uid")
    def _compute_is_skoll_admin(self):
        is_admin = self.env.user.has_group("skoll.group_skoll_pl")
        for rec in self:
            rec.is_skoll_admin = is_admin

    def _search_is_skoll_admin(self, operator, value):
        is_admin = self.env.user.has_group("skoll.group_skoll_pl")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [(1, "=", 1)] if is_admin else [(0, "=", 1)]
        return [(0, "=", 1)] if is_admin else [(1, "=", 1)]

    @staticmethod
    def _sanitize_name(name):
        if not name:
            return name
        name = name.strip().lower()
        name = re.sub(r"[^a-z0-9]+", "-", name)
        return name.strip("-")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "name" in vals:
                vals["name"] = self._sanitize_name(vals["name"])
        return super().create(vals_list)

    def write(self, vals):
        if "name" in vals:
            vals["name"] = self._sanitize_name(vals["name"])
        return super().write(vals)
