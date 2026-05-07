from odoo import fields, models


class KenseiTestResult(models.Model):
    _name = "kensei.test.result"
    _description = "Kensei Auto-Generated Test Result"
    _order = "create_date desc, id desc"

    sandbox_id = fields.Many2one(
        "kensei.sandbox", string="Sandbox", ondelete="cascade", index=True
    )
    kensei_id = fields.Many2one(
        related="sandbox_id.kensei_id", store=True, readonly=True
    )
    task_id = fields.Char(
        related="kensei_id.task_id", string="Task ID", store=True, index=True
    )

    model_type = fields.Char(string="Model", index=True)
    session_index = fields.Integer(string="Session #", default=0, index=True)

    model_used = fields.Char(string="LLM Used for Generation")
    status = fields.Selection(
        [
            ("generating", "Generating"),
            ("running", "Running"),
            ("passed", "Passed"),
            ("failed", "Failed"),
            ("error", "Error"),
        ],
        string="Status",
        default="generating",
    )

    test_code = fields.Text(string="Test Code")
    test_output = fields.Text(string="Execution Output")

    tests_total = fields.Integer(string="Total Tests", default=0)
    tests_passed = fields.Integer(string="Passed", default=0)
    tests_failed = fields.Integer(string="Failed", default=0)
    tests_errored = fields.Integer(string="Errors", default=0)

    generation_prompt = fields.Text(string="Generation Prompt")
    generation_tokens_in = fields.Integer(string="Input Tokens", default=0)
    generation_tokens_out = fields.Integer(string="Output Tokens", default=0)
    duration_generation_ms = fields.Float(string="Generation Duration (ms)")
    duration_execution_ms = fields.Float(string="Execution Duration (ms)")
