from odoo import fields, models


class GohanStartTaskWizard(models.TransientModel):
    _name = "gohan.start.task.wizard"
    _description = "Start Task — Pick Category"

    category_id = fields.Many2one(
        "gohan.category",
        string="Website Category",
        help="Leave empty to get any available task.",
    )

    def action_confirm(self):
        """Delegate to gohan.job.action_start_task with category filter."""
        self.ensure_one()
        ctx = {}
        if self.category_id:
            ctx["start_task_category_id"] = self.category_id.id
        return self.env["gohan.job"].with_context(**ctx).action_start_task()
