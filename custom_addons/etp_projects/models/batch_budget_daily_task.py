from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


HEALTH_SELECTION = [
    ("healthy", "Healthy"),
    ("warning", "Warning"),
    ("critical", "Critical"),
    ("unknown", "Unknown"),
]


class EtpBatchBudgetDailyTask(models.Model):
    _name = "etp.batch.budget.daily.task"
    _description = "Phase Budget Daily Task Log"
    _order = "entry_date desc, id desc"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Phase Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    entry_date = fields.Date(
        string="Logged On",
        required=True,
        default=fields.Date.context_today,
    )
    start_date = fields.Datetime(string="Period Start")
    end_date = fields.Datetime(string="Period End")
    connected_model = fields.Char(string="Source Model", readonly=True)
    done_count = fields.Integer(string="Done Tasks", required=True)
    no_of_trajectory = fields.Integer(string="No. of Trajectories")
    per_task_cost = fields.Float(string="Per Task Cost (USD)", required=True)
    per_trajectory_cost = fields.Float(string="Per Trajectory Cost (USD)")
    total_cost = fields.Float(string="Total Cost (USD)")
    ideal_per_task_cost = fields.Float(string="Ideal Per Task Cost (USD)")
    ideal_per_trajectory_cost = fields.Float(
        string="Ideal Per Trajectory Cost (USD)",
    )
    infra_cost = fields.Float(string="Infra Cost (USD)")
    subscription_cost = fields.Float(string="Subscription Cost (USD)")
    health_status = fields.Selection(
        HEALTH_SELECTION,
        string="Health",
        default="unknown",
        required=True,
    )
    note = fields.Char(string="Note")
    model_breakdown_ids = fields.One2many(
        "etp.batch.budget.daily.task.model",
        "daily_task_id",
        string="Model-wise Costing",
    )

    @api.constrains("entry_date", "batch_id")
    def _check_entry_date_within_batch(self):
        for rec in self:
            batch = rec.batch_id
            if not (rec.entry_date and batch):
                continue
            start = batch.start_date
            end = batch.end_date
            if start and rec.entry_date < start:
                raise ValidationError(_(
                    "Daily task date %(date)s is before the phase budget "
                    "start date %(start)s."
                ) % {
                    "date": fields.Date.to_string(rec.entry_date),
                    "start": fields.Date.to_string(start),
                })
            if end and rec.entry_date > end:
                raise ValidationError(_(
                    "Daily task date %(date)s is after the phase budget "
                    "end date %(end)s."
                ) % {
                    "date": fields.Date.to_string(rec.entry_date),
                    "end": fields.Date.to_string(end),
                })

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.model_breakdown_ids or not rec.batch_id.model_line_ids:
                continue
            rec.model_breakdown_ids = [
                (0, 0, {
                    "ai_model_id": line.ai_model_id.id,
                    "cost_type": line.cost_type,
                    "per_task_cost": line.per_task_cost,
                    "per_trajectory_cost": line.per_trajectory_cost,
                    "iterations": line.iterations,
                })
                for line in rec.batch_id.model_line_ids
                if line.ai_model_id
            ]
        batches_to_advance = records.mapped("batch_id").filtered(
            lambda b: b.state == "approved"
        )
        if batches_to_advance:
            batches_to_advance.write({"state": "in_progress"})
        return records
