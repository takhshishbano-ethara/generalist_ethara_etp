from odoo import fields, models


class WildclawTestResult(models.Model):
    _name = "wildclaw.test.result"
    _description = "WildClaw Auto-Generated Test Result (per-sandbox)"
    _order = "create_date desc, id desc"

    sandbox_model = fields.Char(string="Sandbox Model", index=True)
    sandbox_id_int = fields.Integer(string="Sandbox ID", index=True)
    task_id = fields.Char(string="Task ID", index=True)
    trajectory_index = fields.Integer(string="Trajectory #", default=0, index=True)

    model_used = fields.Char(string="LLM Used for Generation")
    status = fields.Selection(
        [
            ("pending", "Pending Execution"),
            ("generating", "Generating"),
            ("running", "Running"),
            ("passed", "Passed"),
            ("failed", "Failed"),
            ("error", "Error"),
        ],
        string="Status",
        default="generating",
    )

    score = fields.Integer(string="Score", default=0)
    test_scores = fields.Text(string="Per-Function Scores", default="{}")

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
