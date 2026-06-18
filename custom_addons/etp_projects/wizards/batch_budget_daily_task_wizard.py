from datetime import datetime, time

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError


WARNING_RATIO = 1.0
CRITICAL_RATIO = 1.1


def _user_today_bounds(env):
    tz = pytz.timezone(env.user.tz or "UTC")
    today_local = fields.Date.context_today(env.user)
    start_local = tz.localize(datetime.combine(today_local, time.min))
    end_local = tz.localize(datetime.combine(today_local, time(23, 59, 59)))
    to_utc = lambda d: d.astimezone(pytz.UTC).replace(tzinfo=None)
    return to_utc(start_local), to_utc(end_local)


class EtpBatchBudgetDailyTaskWizard(models.TransientModel):
    _name = "etp.batch.budget.daily.task.wizard"
    _description = "Batch Budget Daily Task Wizard"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Batch Budget",
        required=True,
        ondelete="cascade",
    )
    connected_model = fields.Char(
        related="batch_id.connected_model",
        string="Source Model",
        readonly=True,
    )
    start_date = fields.Datetime(
        string="Start Date",
        required=True,
        default=lambda self: _user_today_bounds(self.env)[0],
    )
    end_date = fields.Datetime(
        string="End Date",
        required=True,
        default=lambda self: _user_today_bounds(self.env)[1],
    )
    done_count = fields.Integer(string="Done Tasks", readonly=True)
    total_cost = fields.Float(string="Total Cost (USD)")
    per_task_cost = fields.Float(
        string="Per Task Cost (USD)",
        compute="_compute_totals",
    )
    ideal_per_task_cost = fields.Float(
        string="Ideal Per Task Cost (USD)",
        compute="_compute_totals",
    )
    health_status = fields.Selection(
        [
            ("healthy", "Healthy"),
            ("warning", "Warning"),
            ("critical", "Critical"),
            ("unknown", "Unknown"),
        ],
        string="Health",
        compute="_compute_totals",
        default="unknown",
    )
    note = fields.Char(string="Note")

    @api.depends(
        "done_count",
        "total_cost",
        "batch_id.model_line_ids.per_task_cost",
    )
    def _compute_totals(self):
        for wiz in self:
            if wiz.done_count and wiz.total_cost:
                wiz.per_task_cost = wiz.total_cost / wiz.done_count
            else:
                wiz.per_task_cost = 0.0
            wiz.ideal_per_task_cost = sum(
                wiz.batch_id.model_line_ids.mapped("per_task_cost")
            )
            wiz.health_status = wiz._derive_health(
                wiz.per_task_cost, wiz.ideal_per_task_cost
            )

    @staticmethod
    def _derive_health(per_task_cost, ideal):
        if not ideal or not per_task_cost:
            return "unknown"
        ratio = per_task_cost / ideal
        if ratio <= WARNING_RATIO:
            return "healthy"
        if ratio <= CRITICAL_RATIO:
            return "warning"
        return "critical"

    def action_fetch_done_count(self):
        self.ensure_one()
        if not self.connected_model:
            raise UserError(
                _("Pick a project with a connected table before fetching.")
            )
        if not (self.start_date and self.end_date):
            raise UserError(_("Set both start and end dates before fetching."))
        if self.end_date < self.start_date:
            raise UserError(_("End date must be on or after start date."))
        model = self.env.get(self.connected_model)
        if model is None:
            raise UserError(
                _("Model %s is not installed.") % self.connected_model
            )
        self.done_count = model.sudo().search_count(
            [
                ("create_date", ">=", self.start_date),
                ("create_date", "<=", self.end_date),
            ]
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": self.env.context,
        }

    def action_save(self):
        self.ensure_one()
        if not self.done_count:
            raise UserError(_("Fetch the done task count before saving."))
        if not self.total_cost:
            raise UserError(_("Total cost is required."))
        self.env["etp.batch.budget.daily.task"].create(
            {
                "batch_id": self.batch_id.id,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "connected_model": self.connected_model or False,
                "done_count": self.done_count,
                "per_task_cost": self.per_task_cost,
                "total_cost": self.total_cost,
                "ideal_per_task_cost": self.ideal_per_task_cost,
                "health_status": self.health_status,
                "note": self.note or False,
            }
        )
        self._sync_connected_records()
        return {"type": "ir.actions.act_window_close"}

    def _sync_connected_records(self):
        self.ensure_one()
        model_name = self.connected_model
        if not model_name:
            return
        source_model = self.env.get(model_name)
        if source_model is None:
            return
        if not (self.start_date and self.end_date):
            return
        source_ids = source_model.sudo().search(
            [
                ("create_date", ">=", self.start_date),
                ("create_date", "<=", self.end_date),
            ]
        ).ids
        if not source_ids:
            return
        existing_ids = set(
            self.batch_id.connected_record_ids.filtered(
                lambda r: r.connected_model == model_name
            ).mapped("res_id")
        )
        new_ids = [rid for rid in source_ids if rid not in existing_ids]
        if not new_ids:
            return
        self.env["etp.batch.budget.connected.record"].sudo().create([
            {"batch_id": self.batch_id.id, "res_id": rid}
            for rid in new_ids
        ])
