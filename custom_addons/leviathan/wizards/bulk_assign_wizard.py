from odoo import api, fields, models
from odoo.exceptions import UserError


class BulkAssignWizard(models.TransientModel):
    _name = "leviathan.bulk.assign.wizard"
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

        tasks = self.env["leviathan.job"].browse(active_ids)
        tasks.write({"user_id": self.user_id.id})
        return {"type": "ir.actions.act_window_close"}
