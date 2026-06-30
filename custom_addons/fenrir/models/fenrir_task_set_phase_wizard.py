"""Bulk-set the delivery Phase on selected tasks.

Launched from the All Tasks list Action menu (managers only). Reads the
selected task ids from the context (active_ids) and writes the chosen phase
to all of them at once. Works for a single task or many.
"""

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError


class FenrirTaskSetPhaseWizard(models.TransientModel):
    _name = "fenrir.task.set.phase.wizard"
    _description = "Fenrir — Set Phase on Tasks"

    phase_id = fields.Many2one(
        comodel_name="fenrir.phase",
        string="Phase",
        required=True,
        help="Phase to assign to every selected task.")
    task_count = fields.Integer(
        string="Selected Tasks",
        default=lambda self: len(self.env.context.get("active_ids", [])))

    def action_apply(self):
        self.ensure_one()
        if not self.env.user.has_group("fenrir.group_fenrir_manager"):
            raise AccessError(_("Only Fenrir managers can change the Phase."))
        task_ids = self.env.context.get("active_ids", [])
        if not task_ids:
            raise UserError(_("No tasks were selected."))
        self.env["fenrir.task"].browse(task_ids).write(
            {"phase_id": self.phase_id.id})
        return {"type": "ir.actions.act_window_close"}
