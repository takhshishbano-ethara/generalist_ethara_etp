from odoo import _, api, fields, models
from odoo.exceptions import UserError


WARNING_RATIO = 1.0
CRITICAL_RATIO = 1.1


class EtpBatchBudgetDailyTaskWizard(models.TransientModel):
    _name = "etp.batch.budget.daily.task.wizard"
    _description = "Phase Budget Daily Task Wizard"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
        required=True,
        ondelete="cascade",
    )
    connected_model = fields.Char(
        related="batch_id.connected_model",
        string="Source Model",
        readonly=True,
    )
    entry_date = fields.Date(
        string="Date",
        required=True,
        default=fields.Date.context_today,
    )
    done_count = fields.Integer(string="Done Tasks", required=True)
    total_cost = fields.Float(string="Total Cost (USD)", required=True)
    no_of_trajectory = fields.Integer(
        string="No. of Trajectories",
        compute="_compute_totals",
    )
    per_task_cost = fields.Float(
        string="Per Task Cost (USD)",
        compute="_compute_totals",
    )
    per_trajectory_cost = fields.Float(
        string="Per Trajectory Cost (USD)",
        compute="_compute_totals",
    )
    ideal_per_task_cost = fields.Float(
        string="Ideal Per Task Cost (USD)",
        compute="_compute_totals",
    )
    ideal_per_trajectory_cost = fields.Float(
        string="Ideal Per Trajectory Cost (USD)",
        compute="_compute_totals",
    )
    infra_cost = fields.Float(
        string="Infra Cost (USD)",
        compute="_compute_totals",
    )
    subscription_cost = fields.Float(
        string="Subscription Cost (USD)",
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
        "batch_id.model_line_ids.per_trajectory_cost",
        "batch_id.model_line_ids.iterations",
        "batch_id.infra_line_ids.per_day_cost",
        "batch_id.subscription_line_ids.per_day_cost",
    )
    def _compute_totals(self):
        for wiz in self:
            model_lines = wiz.batch_id.model_line_ids
            iterations_per_task = sum(model_lines.mapped("iterations"))
            wiz.no_of_trajectory = (wiz.done_count or 0) * iterations_per_task
            wiz.per_task_cost = (
                (wiz.total_cost / wiz.done_count)
                if (wiz.done_count and wiz.total_cost)
                else 0.0
            )
            wiz.per_trajectory_cost = (
                (wiz.total_cost / wiz.no_of_trajectory)
                if (wiz.no_of_trajectory and wiz.total_cost)
                else 0.0
            )
            wiz.ideal_per_task_cost = sum(model_lines.mapped("per_task_cost"))
            wiz.ideal_per_trajectory_cost = sum(
                model_lines.mapped("per_trajectory_cost")
            )
            wiz.infra_cost = sum(
                wiz.batch_id.infra_line_ids.mapped("per_day_cost")
            )
            wiz.subscription_cost = sum(
                wiz.batch_id.subscription_line_ids.mapped("per_day_cost")
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

    def action_save(self):
        self.ensure_one()
        if not self.done_count:
            raise UserError(_("Done task count is required."))
        if not self.total_cost:
            raise UserError(_("Total cost is required."))
        start = self.batch_id.start_date
        end = self.batch_id.end_date
        if self.entry_date and start and self.entry_date < start:
            raise UserError(_(
                "Date %(date)s is before the phase budget start date "
                "%(start)s."
            ) % {
                "date": fields.Date.to_string(self.entry_date),
                "start": fields.Date.to_string(start),
            })
        if self.entry_date and end and self.entry_date > end:
            raise UserError(_(
                "Date %(date)s is after the phase budget end date "
                "%(end)s."
            ) % {
                "date": fields.Date.to_string(self.entry_date),
                "end": fields.Date.to_string(end),
            })
        self.env["etp.batch.budget.daily.task"].create(
            {
                "batch_id": self.batch_id.id,
                "entry_date": self.entry_date,
                "connected_model": self.connected_model or False,
                "done_count": self.done_count,
                "no_of_trajectory": self.no_of_trajectory,
                "per_task_cost": self.per_task_cost,
                "per_trajectory_cost": self.per_trajectory_cost,
                "total_cost": self.total_cost,
                "ideal_per_task_cost": self.ideal_per_task_cost,
                "ideal_per_trajectory_cost": self.ideal_per_trajectory_cost,
                "infra_cost": self.infra_cost,
                "subscription_cost": self.subscription_cost,
                "health_status": self.health_status,
                "note": self.note or False,
            }
        )
        return {"type": "ir.actions.act_window_close"}
