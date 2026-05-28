from odoo import api, fields, models


class TalosWildclawSandbox(models.Model):
    _name = "talos_wildclaw.sandbox"
    _description = "Talos WildClaw Sandbox"
    _inherit = "wildclaw.sandbox_base"

    wc_task_id = fields.Many2one("talos_wildclaw.task", required=True, ondelete="cascade", index=True)
    wc_employee_ids = fields.Many2many("hr.employee", "talos_wildclaw_sandbox_employee_rel",
                                       "sandbox_id", "employee_id", string="Employees")
    wc_trajectory_jsonl = fields.Text(string="Trajectory JSONL")
    wc_trajectory_status = fields.Selection(
        [("idle", "Idle"), ("running", "Running"), ("done", "Done"), ("error", "Error")],
        default="idle",
    )
    wc_trajectory_error = fields.Text(string="Trajectory Error")
    wc_turn_ids = fields.One2many("talos_wildclaw.turn", "wc_sandbox_id", string="Turns")

    _sql_constraints = [
        ("talos_wildclaw_sandbox_uniq", "UNIQUE(wc_task_id, model_type, variant_index)",
         "Sandbox variant must be unique per (task, model_type)."),
    ]

    def action_retry_pod(self):
        self.write({"docker_status": "starting"})
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Retry", "message": "Restart queued.", "type": "info"}}


class TalosWildclawTurn(models.Model):
    _name = "talos_wildclaw.turn"
    _description = "Talos WildClaw Turn"
    _order = "wc_turn_number"

    wc_task_id = fields.Many2one("talos_wildclaw.task", ondelete="cascade", index=True)
    wc_sandbox_id = fields.Many2one("talos_wildclaw.sandbox", ondelete="cascade", index=True)
    wc_turn_number = fields.Integer(required=True, index=True)
    wc_prompt_timestamp = fields.Datetime()
    wc_response_timestamp = fields.Datetime()
    wc_model_name = fields.Char()
    wc_tool_names = fields.Char()
    wc_tool_calls = fields.Text()
    wc_raw_events = fields.Text()
    wc_prompt = fields.Text()
    wc_response = fields.Text()
    wc_trajectory = fields.Text()
    wc_qc_severity = fields.Selection(
        [("advisory", "Advisory"), ("warning", "Warning"), ("block", "Block")],
    )
    wc_qc_dismiss_reason = fields.Text()
