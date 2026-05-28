from odoo import fields, models


class SkollWildclawTurn(models.Model):
    _name = "skoll_wildclaw.turn"
    _description = "Skoll WildClaw Turn"
    _order = "wc_turn_number"

    wc_task_id = fields.Many2one("skoll_wildclaw.task", ondelete="cascade", index=True)
    wc_sandbox_id = fields.Many2one("skoll_wildclaw.sandbox", ondelete="cascade", index=True)
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
