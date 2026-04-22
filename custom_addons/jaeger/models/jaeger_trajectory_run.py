from odoo import fields, models


class JaegerTrajectoryRun(models.Model):
    _name = "jaeger.trajectory.run"
    _description = "Jaeger Trajectory Run"
    _order = "create_date desc"

    # ── Identity ─────────────────────────────────────────────────────────
    name = fields.Char(string="Run Name")
    instance_id = fields.Many2one(
        "jaeger.instance",
        string="Instance",
        required=True,
        ondelete="cascade",
        index=True,
    )
    repository_id = fields.Many2one(
        "jaeger.repository",
        string="Repository",
        related="instance_id.repository_id",
        store=True,
    )
    run_number = fields.Integer(string="Run Number")
    model = fields.Char(string="LLM Model")
    status = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("evaluating", "Evaluating"),
            ("resolved", "Resolved"),
            ("unresolved", "Unresolved"),
            ("error", "Error"),
        ],
        string="Status",
        default="pending",
        index=True,
    )
    eks_pod_name = fields.Char(string="EKS Pod Name")

    # ── Inference Results ─────────────────────────────────────────────────
    agent_patch = fields.Text(string="Agent Patch")
    conversation_log = fields.Text(string="Conversation Log")
    output_jsonl = fields.Text(string="Output JSONL")
    api_calls = fields.Integer(string="API Calls")
    api_cost = fields.Float(string="API Cost (USD)")
    api_time_seconds = fields.Float(string="API Time (s)")
    prompt_tokens = fields.Integer(string="Prompt Tokens")
    completion_tokens = fields.Integer(string="Completion Tokens")
    duration_seconds = fields.Float(string="Duration (s)")

    # ── Evaluation Results ────────────────────────────────────────────────
    eval_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("resolved", "Resolved"),
            ("unresolved", "Unresolved"),
            ("error", "Error"),
        ],
        string="Eval Status",
        default="pending",
    )
    eval_report_json = fields.Text(string="Eval Report JSON")
    fix_patch_run_log = fields.Text(string="Fix Patch Run Log")
    resolved = fields.Boolean(string="Resolved")
    eval_passed_count = fields.Integer(string="Eval: Passed")
    eval_failed_count = fields.Integer(string="Eval: Failed")
