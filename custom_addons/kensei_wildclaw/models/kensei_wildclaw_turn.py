from odoo import fields, models


class KenseiWildclawTurn(models.Model):
    _name = "kensei_wildclaw.turn"
    _description = "Kensei WildClaw Turn"
    _order = "wc_turn_number"

    wc_task_id = fields.Many2one("kensei_wildclaw.task", index=True, ondelete="cascade")
    wc_sandbox_id = fields.Many2one("kensei_wildclaw.sandbox", index=True, ondelete="cascade")
    wc_turn_number = fields.Integer(string="Turn #", required=True, index=True)
    wc_prompt_timestamp = fields.Datetime(string="Prompt At")
    wc_response_timestamp = fields.Datetime(string="Response At")
    wc_model_name = fields.Char(string="Model")
    wc_tool_names = fields.Char(string="Tools")
    wc_tool_calls = fields.Text(string="Tool Calls")
    wc_raw_events = fields.Text(string="Raw Events")
    wc_prompt = fields.Text(string="Prompt")
    wc_response = fields.Text(string="Response")
    wc_trajectory = fields.Text(string="Trajectory Snippet")
    wc_qc_severity = fields.Selection(
        [("advisory", "Advisory"), ("warning", "Warning"), ("block", "Block")],
        string="QC Severity",
    )
    wc_qc_dismiss_reason = fields.Char(string="QC Dismiss Reason")
