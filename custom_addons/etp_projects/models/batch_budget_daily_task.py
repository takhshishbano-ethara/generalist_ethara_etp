from odoo import api, fields, models


HEALTH_SELECTION = [
    ("healthy", "Healthy"),
    ("warning", "Warning"),
    ("critical", "Critical"),
    ("unknown", "Unknown"),
]


class EtpBatchBudgetDailyTask(models.Model):
    _name = "etp.batch.budget.daily.task"
    _description = "Batch Budget Daily Task Log"
    _order = "entry_date desc, id desc"

    batch_id = fields.Many2one(
        "etp.batch.budget",
        string="Batch Budget",
        required=True,
        ondelete="cascade",
        index=True,
    )
    entry_date = fields.Date(
        string="Logged On",
        required=True,
        default=fields.Date.context_today,
    )
    start_date = fields.Datetime(string="Period Start", required=True)
    end_date = fields.Datetime(string="Period End", required=True)
    connected_model = fields.Char(string="Source Model", readonly=True)
    done_count = fields.Integer(string="Done Tasks", required=True)
    per_task_cost = fields.Float(string="Per Task Cost (USD)", required=True)
    total_cost = fields.Float(string="Total Cost (USD)")
    ideal_per_task_cost = fields.Float(string="Ideal Per Task Cost (USD)")
    health_status = fields.Selection(
        HEALTH_SELECTION,
        string="Health",
        default="unknown",
        required=True,
    )
    note = fields.Char(string="Note")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        batches_to_advance = records.mapped("batch_id").filtered(
            lambda b: b.state == "approved"
        )
        if batches_to_advance:
            batches_to_advance.write({"state": "in_progress"})
        return records
