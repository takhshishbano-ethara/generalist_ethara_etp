"""Delivery phases for Fenrir tasks.

Managers maintain the list of phases (Phase 1, Phase 1.5, Phase 2, ...) and
flag exactly one as the default, which is preselected on new tasks.
"""

import logging

from odoo import api, fields, models


_logger = logging.getLogger(__name__)


class FenrirPhase(models.Model):
    _name = "fenrir.phase"
    _description = "Fenrir Delivery Phase"
    _order = "sequence, name"

    name = fields.Char(
        string="Phase", required=True,
        help='Display name, e.g. "Phase 1", "Phase 1.5", "Phase 2".')
    sequence = fields.Integer(
        string="Sequence", default=10,
        help="Order in which phases are listed.")
    is_default = fields.Boolean(
        string="Default Phase",
        help="Preselected on new tasks. Only one phase can be the default; "
             "setting this clears the flag on the others.")
    active = fields.Boolean(string="Active", default=True)
    task_count = fields.Integer(
        string="Tasks", compute="_compute_task_count")

    _name_unique = models.Constraint(
        "unique(name)",
        "A phase with this name already exists.",
    )

    def _compute_task_count(self):
        Task = self.env["fenrir.task"]
        for rec in self:
            rec.task_count = Task.search_count([("phase_id", "=", rec.id)]) \
                if rec.id else 0

    def _demote_other_defaults(self):
        """Ensure at most one phase is flagged as default."""
        if not self:
            return
        others = self.search([
            ("is_default", "=", True), ("id", "not in", self.ids)])
        if others:
            others.write({"is_default": False})

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if records.filtered("is_default"):
            records.filtered("is_default")[-1:]._demote_other_defaults()
        return records

    def write(self, vals):
        res = super().write(vals)
        if vals.get("is_default"):
            # Keep the last record in self as the sole default.
            self[-1:]._demote_other_defaults()
        return res

    def action_view_tasks(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.name,
            "res_model": "fenrir.task",
            "view_mode": "list,kanban,form",
            "domain": [("phase_id", "=", self.id)],
            "context": {"from_all_tasks": True, "default_phase_id": self.id},
            "target": "current",
        }
