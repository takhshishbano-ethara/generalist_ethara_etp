from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


HEALTH_SELECTION = [
    ("healthy", "Healthy"),
    ("warning", "Warning"),
    ("critical", "Critical"),
    ("unknown", "Unknown"),
]


class EtharaProjectPhaseDailyTask(models.Model):
    _name = "ethara.project.phase.daily.task"
    _description = "Ethara Project Phase Daily Task Log"
    _order = "entry_date desc, id desc"

    phase_id = fields.Many2one(
        "ethara.project.phase",
        string="Phase",
        required=True,
        ondelete="cascade",
        index=True,
    )
    ethara_project_id = fields.Many2one(
        "ethara.project",
        related="phase_id.ethara_project_id",
        store=True,
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
        "ethara.project.phase.daily.task.model",
        "daily_task_id",
        string="Model-wise Costing",
    )

    @api.constrains("entry_date", "phase_id")
    def _check_entry_date_within_phase(self):
        for rec in self:
            phase = rec.phase_id
            if not (rec.entry_date and phase):
                continue
            start = phase.start_date
            end = phase.end_date
            if start and rec.entry_date < start:
                raise ValidationError(_(
                    "Daily task date %(date)s is before the phase "
                    "start date %(start)s."
                ) % {
                    "date": fields.Date.to_string(rec.entry_date),
                    "start": fields.Date.to_string(start),
                })
            if end and rec.entry_date > end:
                raise ValidationError(_(
                    "Daily task date %(date)s is after the phase "
                    "end date %(end)s."
                ) % {
                    "date": fields.Date.to_string(rec.entry_date),
                    "end": fields.Date.to_string(end),
                })

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.model_breakdown_ids or not rec.phase_id.model_line_ids:
                continue
            rec.model_breakdown_ids = [
                (0, 0, {
                    "ai_model_id": line.ai_model_id.id,
                    "ai_model_name": line.ai_model_name or False,
                    "cost_type": line.cost_type,
                    "per_task_cost": line.per_task_cost,
                    "per_trajectory_cost": line.per_trajectory_cost,
                    "iterations": line.iterations,
                })
                for line in rec.phase_id.model_line_ids
                if line.ai_model_id
            ]
        phases_to_advance = records.mapped("phase_id").filtered(
            lambda p: p.state == "approved"
        )
        if phases_to_advance:
            phases_to_advance.write({"state": "in_progress"})
        return records
