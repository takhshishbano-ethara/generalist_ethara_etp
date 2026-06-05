from odoo import api, fields, models
from odoo.exceptions import ValidationError


MAX_RUBRICS_PER_TASK = 60


class FenrirRubric(models.Model):
    _name = "fenrir.rubric"
    _description = "Fenrir Task Rubric"
    _order = "task_id, sequence, id"

    task_id = fields.Many2one(
        comodel_name="fenrir.task",
        string="Task",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Rubric Name", required=True)
    description = fields.Text(string="Description")

    @api.constrains("task_id")
    def _check_max_rubrics_per_task(self):
        affected = self.mapped("task_id")
        for task in affected:
            if len(task.rubric_ids) > MAX_RUBRICS_PER_TASK:
                raise ValidationError(
                    f"Task '{task.code}' has {len(task.rubric_ids)} rubrics; "
                    f"maximum allowed is {MAX_RUBRICS_PER_TASK}.")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        Score = self.env["fenrir.rubric.score"]
        for rec in records:
            for seller in rec.task_id.seller_offer_ids:
                exists = Score.search_count([
                    ("seller_offer_id", "=", seller.id),
                    ("rubric_id", "=", rec.id),
                ])
                if not exists:
                    Score.create({
                        "seller_offer_id": seller.id,
                        "rubric_id": rec.id,
                    })
        return records
