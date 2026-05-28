from odoo import api, fields, models


class SkollWildclawSandbox(models.Model):
    _name = "skoll_wildclaw.sandbox"
    _description = "Skoll WildClaw Sandbox"
    _inherit = "wildclaw.sandbox_base"

    wc_task_id = fields.Many2one("skoll_wildclaw.task", required=True, ondelete="cascade", index=True)
    wc_employee_ids = fields.Many2many("hr.employee", "skoll_wildclaw_sandbox_employee_rel",
                                       "sandbox_id", "employee_id", string="Employees")
    wc_trajectory_jsonl = fields.Text(string="Trajectory JSONL")
    wc_trajectory_status = fields.Selection(
        [("idle", "Idle"), ("running", "Running"), ("done", "Done"), ("error", "Error")],
        default="idle",
    )
    wc_trajectory_error = fields.Text(string="Trajectory Error")
    wc_connection_error = fields.Text(string="Connection Error")
    wc_latest_error_trace = fields.Text(string="Latest Error Trace")
    wc_turn_ids = fields.One2many("skoll_wildclaw.turn", "wc_sandbox_id", string="Turns")
    wc_api_request_ids = fields.One2many("wildclaw.api.request",
                                          compute="_compute_api_requests", string="API Requests")
    wc_test_result_ids = fields.One2many("wildclaw.test.result",
                                          compute="_compute_test_results", string="Test Results")

    _sql_constraints = [
        ("skoll_wildclaw_sandbox_uniq", "UNIQUE(wc_task_id, model_type, variant_index)",
         "Sandbox variant must be unique per (task, model_type)."),
    ]

    def _compute_api_requests(self):
        APIReq = self.env["wildclaw.api.request"]
        for rec in self:
            rec.wc_api_request_ids = APIReq.search([
                ("sandbox_model", "=", rec._name), ("sandbox_id_int", "=", rec.id)
            ])

    def _compute_test_results(self):
        TR = self.env["wildclaw.test.result"]
        for rec in self:
            rec.wc_test_result_ids = TR.search([
                ("sandbox_model", "=", rec._name), ("sandbox_id_int", "=", rec.id)
            ])

    def action_retry_pod(self):
        self.write({"docker_status": "starting"})
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Retry Pod", "message": "Restart queued.", "type": "info"}}

    def action_stop_sandbox(self):
        return self.action_stop_local()
