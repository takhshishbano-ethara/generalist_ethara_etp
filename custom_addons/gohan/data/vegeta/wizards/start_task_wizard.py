from odoo import fields, models


class VegetaStartTaskWizard(models.TransientModel):
    _name = "vegeta.start.task.wizard"
    _description = "Start Task — Pick Category"

    category_id = fields.Many2one(
        "vegeta.category",
        string="Website Category",
        help="Leave empty to get any available task.",
    )

    def action_confirm(self):
        """Delegate to vegeta.job.action_start_task with category filter."""
        self.ensure_one()
        ctx = {}
        if self.category_id:
            ctx["start_task_category_id"] = self.category_id.id
        return self.env["vegeta.job"].with_context(**ctx).action_start_task()
