from odoo import api, fields, models
from odoo.exceptions import UserError


class BulkAssignWizard(models.TransientModel):
    _name = "vegeta.bulk.assign.wizard"
    _description = "Bulk Assign Tasks"

    user_id = fields.Many2one(
        "res.users",
        string="Assign to",
        required=True,
    )
    task_count = fields.Integer(
        string="Tasks Selected",
        compute="_compute_task_count",
    )

    @api.depends_context("active_ids")
    def _compute_task_count(self):
        for rec in self:
            rec.task_count = len(self.env.context.get("active_ids", []))

    def action_assign(self):
        self.ensure_one()
        active_ids = self.env.context.get("active_ids", [])
        if not active_ids:
            raise UserError("No tasks selected.")

        tasks = self.env["vegeta.job"].browse(active_ids)
        eligible = tasks.filtered(lambda t: t.state != "submitted")
        skipped = tasks - eligible
        if not eligible:
            raise UserError(
                "All selected tasks are in 'Submitted' state and cannot be reassigned."
            )

        eligible.write({"user_id": self.user_id.id})

        if not skipped:
            return {"type": "ir.actions.act_window_close"}

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Bulk Assign Complete",
                "message": (
                    f"{len(eligible)} task(s) assigned. "
                    f"{len(skipped)} 'Submitted' task(s) skipped."
                ),
                "type": "warning",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
