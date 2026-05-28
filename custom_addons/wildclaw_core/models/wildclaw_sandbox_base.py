from odoo import fields, models


class WildclawSandboxBase(models.AbstractModel):
    """Abstract base for per-wrapper sandbox models.

    Wrappers concretize this with their own _name (e.g. kensei_wildclaw.sandbox)
    and add Many2one FKs to their concrete task model. Lifecycle logic
    (start/stop/health/trajectory read) lives in services/wildclaw_runner.py
    so it can be reused across wrappers without inheritance gymnastics.
    """

    _name = "wildclaw.sandbox_base"
    _description = "WildClaw Abstract Sandbox Base"
    _order = "create_date desc, id desc"

    MODEL_TYPES = [("claude", "Claude Opus 4.7"), ("gpt", "GPT-5.5")]

    model_type = fields.Selection(MODEL_TYPES, string="Model", required=True, default="claude")
    variant_index = fields.Integer(string="Variant Index", default=0, index=True)

    docker_compose_project = fields.Char(string="Docker Compose Project")
    docker_status = fields.Selection(
        [
            ("stopped", "Stopped"),
            ("starting", "Starting"),
            ("running", "Running"),
            ("stopping", "Stopping"),
            ("error", "Error"),
        ],
        string="Container Status",
        default="stopped",
    )
    docker_port = fields.Integer(string="Gateway Port")
    docker_litellm_port = fields.Integer(string="LiteLLM Port")
    docker_gateway_token = fields.Char(string="Gateway Token")
    docker_workdir = fields.Char(string="Workdir")
    docker_dashboard_url = fields.Char(string="Dashboard URL", compute="_compute_docker_dashboard_url")
    docker_ws_url = fields.Char(string="WebSocket URL", compute="_compute_docker_ws_url")
    docker_error = fields.Text(string="Last Error")

    session_status = fields.Selection(
        [("not_started", "Not Started"), ("in_progress", "In Progress"), ("completed", "Completed")],
        string="Session Status",
        default="not_started",
    )

    auto_hint_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("evaluating", "Evaluating"),
            ("sending_hint", "Sending Hint"),
            ("streaming", "Streaming"),
            ("max_retries", "Max Retries"),
            ("error", "Error"),
        ],
        string="Auto-Hint Status",
        default="idle",
    )
    auto_hint_iteration = fields.Integer(string="Auto-Hint Iteration", default=0)
    auto_hint_group_id = fields.Char(string="Auto-Hint Group ID")

    def _compute_docker_dashboard_url(self):
        for rec in self:
            if rec.docker_port and rec.docker_gateway_token:
                rec.docker_dashboard_url = "http://localhost:%s/#token=%s" % (
                    rec.docker_port, rec.docker_gateway_token,
                )
            else:
                rec.docker_dashboard_url = ""

    def _compute_docker_ws_url(self):
        for rec in self:
            rec.docker_ws_url = ("ws://localhost:%s" % rec.docker_port) if rec.docker_port else ""

    def action_run_local(self):
        from ..services import wildclaw_runner
        for rec in self:
            rec.write({"docker_status": "starting", "docker_error": False, "session_status": "in_progress"})
            try:
                execution = wildclaw_runner.run_task(self.env, rec)
                usage = wildclaw_runner.collect_usage(self.env, rec, execution)
                trajectory_text = ""
                if hasattr(rec, "trajectory_jsonl"):
                    try:
                        from pathlib import Path as _P
                        out_dir = _P(self.env["ir.config_parameter"].sudo().get_param(
                            "wildclaw.sandbox_dir", "/tmp/wildclaw_workspaces"
                        )) / "output" / f"{rec._name.replace('.', '_')}_{rec.id}"
                        chat = out_dir / "chat.jsonl"
                        if chat.exists():
                            trajectory_text = chat.read_text()
                    except Exception:
                        pass
                payload = {
                    "docker_status": "running" if not execution.error else "error",
                    "docker_error": execution.error or False,
                    "session_status": "completed",
                }
                if hasattr(rec, "trajectory_jsonl") and trajectory_text:
                    payload["trajectory_jsonl"] = trajectory_text
                    payload["trajectory_status"] = "done" if not execution.error else "error"
                rec.write(payload)
            except Exception as exc:
                rec.write({
                    "docker_status": "error",
                    "docker_error": str(exc),
                    "session_status": "completed",
                })
        return True

    def action_stop_local(self):
        import sys
        from pathlib import Path as _P
        vendor_dir = _P(__file__).resolve().parent.parent / "vendor" / "wildclawbench"
        if str(vendor_dir) not in sys.path:
            sys.path.insert(0, str(vendor_dir))
        try:
            from src.utils import docker_utils  # type: ignore
        except Exception:
            docker_utils = None
        for rec in self:
            task_id = f"{rec._name}_{rec.id}_{rec.model_type}"
            if docker_utils is not None:
                try:
                    docker_utils.remove_container(task_id)
                except Exception:
                    pass
            rec.write({"docker_status": "stopped"})
        return True
