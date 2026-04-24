import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from odoo import models, fields, api, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

from .atlas import (
    _load_dotenv,
    _docker_available,
    _compose_cmd,
    _module_sandbox_dir,
    _DEFAULT_LITELLM_CONFIG,
    _HEALTH_WAIT_TIMEOUT,
    _HEALTH_POLL_INTERVAL,
)

_logger = logging.getLogger(__name__)

_SANDBOX_POOL_WORKERS = int(os.getenv("SANDBOX_POOL_WORKERS", "3"))
_SANDBOX_POOL = ThreadPoolExecutor(
    max_workers=_SANDBOX_POOL_WORKERS, thread_name_prefix="atlas-sandbox"
)
_SANDBOX_STARTING = set()
_SANDBOX_LOCK = threading.Lock()

_GENERATION_POOL = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="atlas-generation"
)

MODEL_TYPES = [
    ("glm", "GLM 5"),
]

MODEL_DEFAULTS = {
    "glm": "litellm/glm-5",
}

GATEWAY_PORT_BASE = 19000
LITELLM_PORT_BASE = 14000
DB_PORT_BASE = 15432



def _run_sandbox_start_background(db_name, sandbox_id, mode, notify_partner_id):
    """Background worker: start sandbox (docker compose or K8s), then notify via bus.bus."""
    final_status = "error"
    error_msg = ""
    model_type = ""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            sandbox = env["atlas.sandbox"].browse(sandbox_id)
            if not sandbox.exists():
                _logger.error(
                    "Background sandbox start: sandbox %s does not exist", sandbox_id
                )
                return
            model_type = sandbox.model_type or ""

        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                sandbox = env["atlas.sandbox"].browse(sandbox_id)
                if mode == "k8s":
                    sandbox._start_k8s_bg()
                else:
                    sandbox._start_local_bg()
        except Exception as e:
            _logger.exception(
                "Background sandbox start failed for sandbox %s: %s",
                sandbox_id,
                e,
            )
            error_msg = str(e)[:1000]

        for attempt in range(3):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    sandbox = env["atlas.sandbox"].browse(sandbox_id)
                    if not sandbox.exists():
                        return

                    if error_msg and sandbox.docker_status != "running":
                        sandbox.write(
                            {
                                "docker_status": "error",
                                "docker_error": error_msg,
                            }
                        )

                    final_status = sandbox.docker_status
                    error_msg = sandbox.docker_error or ""

                    partner = None
                    if notify_partner_id:
                        partner = env["res.partner"].browse(notify_partner_id)
                        if not partner.exists():
                            partner = None
                    if not partner:
                        partner = sandbox.employee_id.user_id.partner_id
                    if partner:
                        env["bus.bus"]._sendone(
                            partner,
                            "atlas/sandbox_ready",
                            {
                                "sandbox_id": sandbox_id,
                                "docker_status": final_status,
                                "error": error_msg,
                                "model_type": model_type,
                            },
                        )
                break
            except Exception as e:
                if "serialize" in str(e).lower() and attempt < 2:
                    _logger.warning(
                        "Serialization conflict in Phase 3 for sandbox %s, retry %d",
                        sandbox_id,
                        attempt + 1,
                    )
                    time.sleep(1 + attempt)
                    continue
                raise
    except Exception:
        _logger.exception("Background sandbox start crashed (sandbox=%s)", sandbox_id)
    finally:
        with _SANDBOX_LOCK:
            _SANDBOX_STARTING.discard(sandbox_id)


def _get_current_session_turns(task):
    glm_sandbox = task.sandbox_ids.filtered(lambda s: s.model_type == "glm")[:1]
    if glm_sandbox and glm_sandbox.current_session_id:
        session_turns = task.turn_ids.filtered(
            lambda t, sid=glm_sandbox.current_session_id: t.session_id == sid
        ).sorted("turn_number")
        if session_turns:
            return session_turns
    return task.turn_ids.sorted("turn_number")


def _run_generation_background(db_name, task_id, notify_partner_id):
    """Background worker: generate goal description + rubric criteria, then notify via bus."""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["atlas.atlas"].browse(task_id)
            if not task.exists():
                _logger.error("Generation bg: task %s does not exist", task_id)
                return

            turns = _get_current_session_turns(task)

            _logger.info(
                "Generation bg: task=%s total_turns=%d turns_with_prompt=%d turns_with_response=%d",
                task_id, len(turns),
                len([t for t in turns if t.prompt and not t.is_hint_turn]),
                len([t for t in turns if t.prompt and not t.is_hint_turn and (t.response or t.turn_status == "Completed")]),
            )

            if not turns:
                task.write({
                    "goal_generation_status": "done",
                    "rubric_generation_status": "done",
                })
                return

            from .atlas import generate_description_from_turns, generate_rubric_from_turns

            # --- Goal description ---
            try:
                task.write({"goal_generation_status": "running"})
                cr.commit()

                desc, desc_usage = generate_description_from_turns(env, turns)
                if desc:
                    write_vals = {"goal_description": desc, "goal_generation_status": "done"}
                    d_in = desc_usage.get("input_tokens", 0)
                    d_out = desc_usage.get("output_tokens", 0)
                    if d_in or d_out:
                        write_vals["goal_input_tokens"] = (task.goal_input_tokens or 0) + d_in
                        write_vals["goal_output_tokens"] = (task.goal_output_tokens or 0) + d_out
                    task.sudo().write(write_vals)
                else:
                    task.write({"goal_generation_status": "done"})
                cr.commit()
            except Exception:
                _logger.exception("Generation bg: goal description failed for task=%s", task_id)
                task.write({"goal_generation_status": "error"})
                cr.commit()

            # --- Rubric criteria ---
            try:
                task.write({"rubric_generation_status": "running"})
                cr.commit()

                criteria_data, rubric_usage = generate_rubric_from_turns(env, turns, task_id=task_id)
                if rubric_usage:
                    r_in = rubric_usage.get("input_tokens", 0)
                    r_out = rubric_usage.get("output_tokens", 0)
                    if r_in or r_out:
                        task.sudo().write({
                            "rubric_input_tokens": (task.rubric_input_tokens or 0) + r_in,
                            "rubric_output_tokens": (task.rubric_output_tokens or 0) + r_out,
                        })

                if criteria_data:
                    task.sudo().rubric_criterion_ids.unlink()
                    valid_cats = ("factuality_hallucination", "task_completion", "instruction_following", "communication_style", "other")
                    valid_imps = ("critically_detrimental", "detrimental", "slightly_detrimental", "slightly_important", "important", "critically_important")
                    created_count = 0
                    for c in criteria_data:
                        if not isinstance(c, dict) or not c.get("name"):
                            continue
                        levels = c.get("levels", [])
                        cat = c.get("category", "other")
                        if cat not in valid_cats:
                            cat = "other"
                        imp = c.get("importance", "important")
                        if imp not in valid_imps:
                            imp = "important"
                        criterion = env["atlas.rubric.criterion"].sudo().create({
                            "atlas_id": task.id,
                            "name": c["name"],
                            "category": cat,
                            "importance": imp,
                            "weight": int(c.get("weight", 5)),
                            "is_negative": bool(c.get("is_negative", False)),
                            "suggestion": c.get("suggestion", ""),
                        })
                        criterion.level_ids.unlink()
                        for lv in levels:
                            if isinstance(lv, dict):
                                env["atlas.rubric.level"].sudo().create({
                                    "criterion_id": criterion.id,
                                    "score": int(lv.get("score", 0)),
                                    "label": lv.get("label", ""),
                                })
                        created_count += 1
                    _logger.info(
                        "Generation bg: created %d rubric criteria for task=%s",
                        created_count, task_id,
                    )
                    if created_count == 0:
                        _logger.warning(
                            "Generation bg: criteria_data had entries but none valid for task=%s",
                            task_id,
                        )
                else:
                    _logger.warning(
                        "Generation bg: rubric parser returned empty criteria for task=%s",
                        task_id,
                    )

                task.write({"rubric_generation_status": "done"})
                cr.commit()
            except Exception:
                _logger.exception("Generation bg: rubric generation failed for task=%s", task_id)
                task.write({"rubric_generation_status": "error"})
                cr.commit()

            # --- Notify frontend via bus ---
            partner = None
            if notify_partner_id:
                partner = env["res.partner"].browse(notify_partner_id)
                if not partner.exists():
                    partner = None
            if partner:
                env["bus.bus"]._sendone(
                    partner,
                    "atlas/generation_done",
                    {
                        "task_id": task_id,
                        "goal_status": task.goal_generation_status,
                        "rubric_status": task.rubric_generation_status,
                    },
                )
            _logger.info(
                "Generation bg: completed for task=%s goal=%s rubric=%s",
                task_id, task.goal_generation_status, task.rubric_generation_status,
            )
    except Exception:
        _logger.exception("Generation bg: unhandled error for task=%s", task_id)
        try:
            with Registry(db_name).cursor() as cr2:
                env2 = api.Environment(cr2, SUPERUSER_ID, {})
                t = env2["atlas.atlas"].browse(task_id)
                if t.exists():
                    vals = {}
                    if t.goal_generation_status == "running":
                        vals["goal_generation_status"] = "error"
                    if t.rubric_generation_status == "running":
                        vals["rubric_generation_status"] = "error"
                    if vals:
                        t.write(vals)
        except Exception:
            _logger.exception("Generation bg: failed to reset status for task=%s", task_id)


def _run_goal_only_background(db_name, task_id, notify_partner_id):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["atlas.atlas"].browse(task_id)
            if not task.exists():
                return

            turns = _get_current_session_turns(task)
            if not turns:
                task.write({"goal_generation_status": "done"})
                return

            from .atlas import generate_description_from_turns

            try:
                task.write({"goal_generation_status": "running"})
                cr.commit()

                desc, desc_usage = generate_description_from_turns(env, turns)
                if desc:
                    write_vals = {"goal_description": desc, "goal_generation_status": "done"}
                    d_in = desc_usage.get("input_tokens", 0)
                    d_out = desc_usage.get("output_tokens", 0)
                    if d_in or d_out:
                        write_vals["goal_input_tokens"] = (task.goal_input_tokens or 0) + d_in
                        write_vals["goal_output_tokens"] = (task.goal_output_tokens or 0) + d_out
                    task.sudo().write(write_vals)
                else:
                    task.write({"goal_generation_status": "done"})
                cr.commit()
            except Exception:
                _logger.exception("Goal-only bg: failed for task=%s", task_id)
                task.write({"goal_generation_status": "error"})
                cr.commit()

            partner = None
            if notify_partner_id:
                partner = env["res.partner"].browse(notify_partner_id)
                if not partner.exists():
                    partner = None
            if partner:
                env["bus.bus"]._sendone(
                    partner,
                    "atlas/generation_done",
                    {
                        "task_id": task_id,
                        "goal_status": task.goal_generation_status,
                        "rubric_status": task.rubric_generation_status,
                    },
                )
    except Exception:
        _logger.exception("Goal-only bg: unhandled error for task=%s", task_id)
        try:
            with Registry(db_name).cursor() as cr2:
                env2 = api.Environment(cr2, SUPERUSER_ID, {})
                t = env2["atlas.atlas"].browse(task_id)
                if t.exists() and t.goal_generation_status == "running":
                    t.write({"goal_generation_status": "error"})
        except Exception:
            _logger.exception("Goal-only bg: failed to reset status for task=%s", task_id)


def _run_rubric_only_background(db_name, task_id, notify_partner_id):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["atlas.atlas"].browse(task_id)
            if not task.exists():
                return

            turns = _get_current_session_turns(task)
            if not turns:
                task.write({"rubric_generation_status": "done"})
                return

            from .atlas import generate_rubric_from_turns

            try:
                task.write({"rubric_generation_status": "running"})
                cr.commit()

                criteria_data, rubric_usage = generate_rubric_from_turns(env, turns, task_id=task_id)
                if rubric_usage:
                    r_in = rubric_usage.get("input_tokens", 0)
                    r_out = rubric_usage.get("output_tokens", 0)
                    if r_in or r_out:
                        task.sudo().write({
                            "rubric_input_tokens": (task.rubric_input_tokens or 0) + r_in,
                            "rubric_output_tokens": (task.rubric_output_tokens or 0) + r_out,
                        })

                if criteria_data:
                    task.sudo().rubric_criterion_ids.unlink()
                    valid_cats = ("factuality_hallucination", "task_completion", "instruction_following", "communication_style", "other")
                    valid_imps = ("critically_detrimental", "detrimental", "slightly_detrimental", "slightly_important", "important", "critically_important")
                    created_count = 0
                    for c in criteria_data:
                        if not isinstance(c, dict) or not c.get("name"):
                            continue
                        levels = c.get("levels", [])
                        cat = c.get("category", "other")
                        if cat not in valid_cats:
                            cat = "other"
                        imp = c.get("importance", "important")
                        if imp not in valid_imps:
                            imp = "important"
                        criterion = env["atlas.rubric.criterion"].sudo().create({
                            "atlas_id": task.id,
                            "name": c["name"],
                            "category": cat,
                            "importance": imp,
                            "weight": int(c.get("weight", 5)),
                            "is_negative": bool(c.get("is_negative", False)),
                            "suggestion": c.get("suggestion", ""),
                        })
                        criterion.level_ids.unlink()
                        for lv in levels:
                            if isinstance(lv, dict):
                                env["atlas.rubric.level"].sudo().create({
                                    "criterion_id": criterion.id,
                                    "score": int(lv.get("score", 0)),
                                    "label": lv.get("label", ""),
                                })
                        created_count += 1
                    _logger.info(
                        "Rubric-only bg: created %d criteria for task=%s",
                        created_count, task_id,
                    )
                else:
                    _logger.warning(
                        "Rubric-only bg: parser returned empty criteria for task=%s",
                        task_id,
                    )

                task.write({"rubric_generation_status": "done"})
                cr.commit()
            except Exception:
                _logger.exception("Rubric-only bg: failed for task=%s", task_id)
                task.write({"rubric_generation_status": "error"})
                cr.commit()

            partner = None
            if notify_partner_id:
                partner = env["res.partner"].browse(notify_partner_id)
                if not partner.exists():
                    partner = None
            if partner:
                env["bus.bus"]._sendone(
                    partner,
                    "atlas/generation_done",
                    {
                        "task_id": task_id,
                        "goal_status": task.goal_generation_status,
                        "rubric_status": task.rubric_generation_status,
                    },
                )
    except Exception:
        _logger.exception("Rubric-only bg: unhandled error for task=%s", task_id)
        try:
            with Registry(db_name).cursor() as cr2:
                env2 = api.Environment(cr2, SUPERUSER_ID, {})
                t = env2["atlas.atlas"].browse(task_id)
                if t.exists() and t.rubric_generation_status == "running":
                    t.write({"rubric_generation_status": "error"})
        except Exception:
            _logger.exception("Rubric-only bg: failed to reset status for task=%s", task_id)


class AtlasSandbox(models.Model):
    _name = "atlas.sandbox"
    _description = "Atlas Sandbox"
    _order = "model_type"

    atlas_id = fields.Many2one(
        "atlas.atlas", required=True, ondelete="cascade", index=True
    )
    employee_id = fields.Many2one(
        related="atlas_id.employee_id", store=True, readonly=True
    )
    model_type = fields.Selection(MODEL_TYPES, required=True, readonly=True)

    docker_compose_project = fields.Char(readonly=True, copy=False)
    docker_status = fields.Selection(
        [
            ("stopped", "Stopped"),
            ("starting", "Starting"),
            ("running", "Running"),
            ("error", "Error"),
        ],
        default="stopped",
        readonly=True,
    )
    docker_port = fields.Integer(readonly=True)
    docker_litellm_port = fields.Integer(readonly=True)
    docker_gateway_token = fields.Char(readonly=True, copy=False)
    docker_dashboard_url = fields.Char(compute="_compute_dashboard_url")
    docker_ws_url = fields.Char(compute="_compute_docker_ws_url")
    docker_error = fields.Text(readonly=True)
    docker_workdir = fields.Char(readonly=True, copy=False)

    session_status = fields.Selection(
        [
            ("not_started", "Not Started"),
            ("in_progress", "In Progress"),
            ("completed", "Completed"),
        ],
        default="not_started",
    )

    current_session_id = fields.Char(string="Current Session ID", copy=False)

    turn_ids = fields.One2many("atlas.turn", "sandbox_id", string="Turns")

    _sql_constraints = [
        (
            "unique_task_model",
            "UNIQUE(atlas_id, model_type)",
            "Each task can only have one sandbox per model type.",
        ),
    ]

    # ------------------------------------------------------------------
    # Computed fields
    # ------------------------------------------------------------------

    def _deployment_mode(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("atlas.deployment_mode", "local")
            .strip()
        )

    @api.depends(
        "docker_port", "docker_gateway_token", "docker_status", "docker_compose_project"
    )
    def _compute_dashboard_url(self):
        for rec in self:
            if rec.docker_status != "running" or not rec.docker_gateway_token:
                rec.docker_dashboard_url = False
                continue

            mode = rec._deployment_mode()
            if mode == "k8s":
                ws_host = (
                    rec.env["ir.config_parameter"]
                    .sudo()
                    .get_param("atlas.ws_router_host", "")
                    .strip()
                )
                if ws_host:
                    rec.docker_dashboard_url = "https://%s/sandbox/%s/#token=%s" % (
                        ws_host,
                        rec.id,
                        rec.docker_gateway_token,
                    )
                else:
                    svc_name = "atlas-sandbox-%s" % rec.id
                    rec.docker_dashboard_url = (
                        "http://%s.atlas.svc.cluster.local:18789/#token=%s"
                        % (svc_name, rec.docker_gateway_token)
                    )
            else:
                if rec.docker_port:
                    rec.docker_dashboard_url = "http://localhost:%d/#token=%s" % (
                        rec.docker_port,
                        rec.docker_gateway_token,
                    )
                else:
                    rec.docker_dashboard_url = False

    @api.depends("docker_port", "docker_status")
    def _compute_docker_ws_url(self):
        for rec in self:
            if rec.docker_status != "running" or not rec.docker_port:
                rec.docker_ws_url = False
                continue

            mode = rec._deployment_mode()
            if mode == "k8s":
                ws_host = (
                    self.env["ir.config_parameter"]
                    .sudo()
                    .get_param("atlas.ws_router_host", "")
                    .strip()
                )
                if ws_host:
                    rec.docker_ws_url = "wss://%s/sandbox/%s/" % (ws_host, rec.id)
                else:
                    rec.docker_ws_url = False
            else:
                rec.docker_ws_url = "ws://localhost:%d" % rec.docker_port

    def _get_gateway_ws_url(self):
        self.ensure_one()
        mode = self._deployment_mode()
        if mode == "k8s":
            svc_name = "atlas-sandbox-%s" % self.id
            return "ws://%s.atlas.svc.cluster.local:18789" % svc_name
        else:
            if not self.docker_port:
                return False
            return "ws://localhost:%d" % self.docker_port

    # ------------------------------------------------------------------
    # Port allocation
    # ------------------------------------------------------------------

    def _allocate_ports(self):
        self.ensure_one()
        offset = self.id % 5000
        return (
            GATEWAY_PORT_BASE + offset,
            LITELLM_PORT_BASE + offset,
            DB_PORT_BASE + offset,
        )
    _INTERNAL_MSG_FIELDS = {
        "sender",
        "thinkingSignature",
        "api",
        "provider",
        "model",
        "usage",
    }
    _INTERNAL_BLOCK_FIELDS = {"api", "provider", "model", "usage"}
    def _query_litellm_spend(self):
        self.ensure_one()
        import urllib.request
        import urllib.error
        import urllib.parse

        mode = self._deployment_mode()
        litellm_key = ""

        if mode == "k8s":
            ws_host = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("atlas.ws_router_host", "")
                .strip()
            )
            if not ws_host:
                _logger.warning(
                    "No ws_router_host configured, cannot query LiteLLM spend (sandbox=%s)",
                    self.id,
                )
                return 0, 0
            base_url = "https://%s/litellm/%s" % (ws_host, self.id)
            dotenv = _load_dotenv()
            litellm_key = dotenv.get("LITELLM_MASTER_KEY", "").strip()
            if not litellm_key:
                litellm_key = (
                    "sk-atlas-%s" % self.docker_gateway_token[:16]
                    if self.docker_gateway_token
                    else ""
                )
        else:
            litellm_port = self.docker_litellm_port
            if not litellm_port:
                return 0, 0
            base_url = "http://localhost:%d" % litellm_port
            dotenv = _load_dotenv()
            litellm_key = dotenv.get("LITELLM_MASTER_KEY", "").strip()

        if not litellm_key:
            _logger.warning(
                "No LITELLM_MASTER_KEY, cannot query LiteLLM spend (sandbox=%s)",
                self.id,
            )
            return 0, 0

        try:
            start_date = (
                self.create_date.strftime("%Y-%m-%d") if self.create_date else ""
            )
            if not start_date:
                return 0, 0

            from datetime import datetime as dt, timedelta

            end_date = (dt.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            params = urllib.parse.urlencode(
                {
                    "start_date": start_date,
                    "end_date": end_date,
                }
            )
            url = "%s/spend/logs?%s" % (base_url, params)

            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": "Bearer %s" % litellm_key,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            logs = data if isinstance(data, list) else data.get("data", [])

            total_in = 0
            total_out = 0
            for entry in logs:
                total_in += int(entry.get("prompt_tokens", 0) or 0)
                total_out += int(entry.get("completion_tokens", 0) or 0)

            _logger.info(
                "LiteLLM spend query returned %d logs (in=%d, out=%d) for sandbox %s",
                len(logs),
                total_in,
                total_out,
                self.id,
            )
            return total_in, total_out

        except urllib.error.HTTPError as e:
            _logger.warning(
                "LiteLLM spend API error for sandbox %s: %s %s",
                self.id,
                e.code,
                e.reason,
            )
            return 0, 0
        except Exception as e:
            _logger.warning(
                "LiteLLM spend query failed for sandbox %s: %s",
                self.id,
                e,
            )
            return 0, 0
    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    def action_start_sandbox(self):
        """Start sandbox asynchronously — returns immediately, work runs in background."""
        self.ensure_one()

        if not self.atlas_id:
            raise UserError(
                "Sandbox is not linked to a task (sandbox_id=%s)." % self.id
            )
        if self.docker_status in ("starting", "running"):
            raise UserError("Sandbox is already %s." % self.docker_status)

        mode = self._deployment_mode()

        if mode != "k8s":
            if not _docker_available():
                raise UserError(
                    "Docker is not available on this server. "
                    "Please ensure the Docker daemon is running."
                )
            if not _compose_cmd():
                raise UserError("docker compose (or docker-compose) not found.")

        with _SANDBOX_LOCK:
            if self.id in _SANDBOX_STARTING:
                raise UserError("Sandbox start is already in progress.")
            _SANDBOX_STARTING.add(self.id)

        gateway_token = secrets.token_hex(32)
        new_session_id = secrets.token_hex(8)
        write_vals = {
            "docker_status": "starting",
            "docker_error": False,
            "docker_gateway_token": gateway_token,
            "current_session_id": new_session_id,
            "session_status": "not_started",
        }
        if mode != "k8s":
            gateway_port, litellm_port, db_port = self._allocate_ports()
            write_vals["docker_port"] = gateway_port
            write_vals["docker_litellm_port"] = litellm_port
        self.write(write_vals)

        sandbox_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        @self.env.cr.postcommit.add
        def _queue_sandbox_start():
            _SANDBOX_POOL.submit(
                _run_sandbox_start_background,
                db_name,
                sandbox_id,
                mode,
                notify_partner_id,
            )

        _logger.info(
            "[SANDBOX] action_start_sandbox | sandbox=%s | model=%s | mode=%s | "
            "queued to background pool",
            self.id,
            self.model_type,
            mode,
        )

    def action_stop_sandbox(self):
        self.ensure_one()

        if self.atlas_id:
            self._collect_glm_tokens()
            self._submit_generation_background()

        mode = self._deployment_mode()
        if mode == "k8s":
            self._stop_k8s()
        else:
            self._stop_local()

    def _collect_glm_tokens(self):
        task = self.atlas_id
        if not task:
            return
        try:
            _logger.info(
                "Querying LiteLLM spend for sandbox=%s litellm_port=%s",
                self.id, self.docker_litellm_port,
            )
            total_in, total_out = self._query_litellm_spend()
            _logger.info(
                "LiteLLM spend result: in=%d out=%d for sandbox=%s",
                total_in, total_out, self.id,
            )
            if total_in > 0 or total_out > 0:
                task.sudo().write({
                    "glm_input_tokens": (task.glm_input_tokens or 0) + total_in,
                    "glm_output_tokens": (task.glm_output_tokens or 0) + total_out,
                })
        except Exception as e:
            _logger.exception("Failed to collect GLM tokens for sandbox=%s", self.id)

    def _submit_generation_background(self):
        task = self.atlas_id
        if not task:
            return

        all_turns = task.turn_ids.sorted("turn_number")
        if not all_turns:
            _logger.warning("[GENERATION] No turns found for task=%s, skipping", task.id)
            return

        task.sudo().write({
            "goal_generation_status": "running",
            "rubric_generation_status": "running",
        })

        task_id = task.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        @self.env.cr.postcommit.add
        def _queue_generation():
            _GENERATION_POOL.submit(
                _run_generation_background,
                db_name,
                task_id,
                notify_partner_id,
            )

        _logger.info(
            "[GENERATION] Queued background goal+rubric generation for task=%s total_turns=%d",
            task_id, len(all_turns),
        )

    def _start_k8s(self):
        if self.docker_status == "running":
            raise UserError("Sandbox is already running.")

        gateway_token = secrets.token_hex(32)
        self.write(
            {
                "docker_status": "starting",
                "docker_gateway_token": gateway_token,
                "docker_error": False,
            }
        )

        try:
            self.env["atlas.sandbox.k8s"].deploy_sandbox(self)
            svc_name = "atlas-sandbox-%s" % self.id
            self.write(
                {
                    "docker_compose_project": svc_name,
                    "docker_status": "starting",
                    "docker_port": 18789,
                }
            )
            _logger.info(
                "Deployed K8s sandbox %s for sandbox %s (model=%s)",
                svc_name,
                self.id,
                self.model_type,
            )
        except Exception as e:
            _logger.error("K8s sandbox deploy failed for sandbox %s: %s", self.id, e)
            self.write({"docker_status": "error", "docker_error": str(e)[:1000]})

    def _stop_k8s(self):
        if self.docker_status == "stopped":
            return

        try:
            self.env["atlas.sandbox.k8s"].destroy_sandbox(self)
            _logger.info("Destroyed K8s sandbox for sandbox %s", self.id)
        except Exception as e:
            _logger.warning("K8s sandbox destroy failed for sandbox %s: %s", self.id, e)

        self.write(
            {
                "docker_compose_project": False,
                "docker_status": "stopped",
                "docker_port": 0,
                "docker_litellm_port": 0,
                "docker_gateway_token": False,
                "docker_error": False,
            }
        )

    def _start_local(self):
        if self.docker_status == "running" and self.docker_compose_project:
            raise UserError("Docker stack is already running for this sandbox.")

        if not _docker_available():
            raise UserError(
                "Docker is not available on this server. "
                "Please ensure the Docker daemon is running."
            )

        compose_bin = _compose_cmd()
        if not compose_bin:
            raise UserError("docker compose (or docker-compose) not found.")

        gateway_token = secrets.token_hex(32)
        project_name = "atlas-%d-%s" % (self.atlas_id.id, self.model_type)
        gateway_port, litellm_port, db_port = self._allocate_ports()

        try:
            workdir = self._prepare_workdir(
                gateway_token, gateway_port, litellm_port, db_port
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "Failed to prepare sandbox: %s" % str(e)[:500],
                }
            )
            return

        compose_env = self._build_compose_env(gateway_token)

        self.write(
            {
                "docker_compose_project": project_name,
                "docker_status": "starting",
                "docker_port": gateway_port,
                "docker_litellm_port": litellm_port,
                "docker_gateway_token": gateway_token,
                "docker_workdir": workdir,
                "docker_error": False,
            }
        )
        self.env.cr.commit()

        cmd = compose_bin + [
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.override.yml",
            "-p",
            project_name,
            "up",
            "-d",
            "--build",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
                cwd=workdir,
                env=compose_env,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": "Compose up failed (exit %d): %s"
                        % (result.returncode, error_msg[:1000]),
                    }
                )
                return

            healthy = self._wait_for_health(compose_bin, project_name, workdir)

            if healthy:
                self.write({"docker_status": "running"})
                _logger.info(
                    "Started sandbox (project=%s) sandbox=%s model=%s",
                    project_name,
                    self.id,
                    self.model_type,
                )
            else:
                logs = self._capture_container_logs(compose_bin, project_name, workdir)
                error_detail = (
                    "Sandbox containers started but the gateway never became "
                    "healthy within %d seconds." % _HEALTH_WAIT_TIMEOUT
                )
                if logs:
                    error_detail += (
                        "\n\nContainer logs (last 30 lines):\n%s" % logs[:2000]
                    )
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": error_detail[:4000],
                    }
                )
                _logger.error(
                    "Gateway health-check failed for project %s (sandbox %s)",
                    project_name,
                    self.id,
                )

        except subprocess.TimeoutExpired:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "docker compose up timed out after 900 seconds",
                }
            )
        except Exception as e:
            self.write({"docker_status": "error", "docker_error": str(e)[:500]})

    def _start_local_bg(self):
        """Start local Docker sandbox — called from background thread."""
        compose_bin = _compose_cmd()
        gateway_token = self.docker_gateway_token
        gateway_port = self.docker_port
        litellm_port = self.docker_litellm_port
        db_port = DB_PORT_BASE + (self.id % 5000)
        project_name = "atlas-%d-%s" % (self.atlas_id.id, self.model_type)

        try:
            workdir = self._prepare_workdir(
                gateway_token, gateway_port, litellm_port, db_port
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "Failed to prepare sandbox: %s" % str(e)[:500],
                }
            )
            return

        compose_env = self._build_compose_env(gateway_token)

        cmd = compose_bin + [
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.override.yml",
            "-p",
            project_name,
            "up",
            "-d",
            "--build",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
                cwd=workdir,
                env=compose_env,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": "Compose up failed: %s" % error_msg[:1000],
                    }
                )
                return

            self.write(
                {
                    "docker_compose_project": project_name,
                    "docker_workdir": workdir,
                }
            )

            healthy = self._wait_for_health(compose_bin, project_name, workdir)

            if healthy:
                self.write({"docker_status": "running"})
                _logger.info(
                    "Started sandbox (project=%s) sandbox=%s model=%s",
                    project_name,
                    self.id,
                    self.model_type,
                )
            else:
                logs = self._capture_container_logs(compose_bin, project_name, workdir)
                error_detail = (
                    "Sandbox containers started but the gateway never became "
                    "healthy within %d seconds." % _HEALTH_WAIT_TIMEOUT
                )
                if logs:
                    error_detail += (
                        "\n\nContainer logs (last 30 lines):\n%s" % logs[:2000]
                    )
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": error_detail[:4000],
                    }
                )

        except subprocess.TimeoutExpired:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "docker compose up timed out after 900 seconds",
                }
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": str(e)[:500],
                }
            )

    def _start_k8s_bg(self):
        """Start K8s sandbox — called from background thread."""
        try:
            self.env["atlas.sandbox.k8s"].deploy_sandbox(self)
            svc_name = "atlas-sandbox-%s" % self.id
            self.write(
                {
                    "docker_compose_project": svc_name,
                    "docker_port": 18789,
                }
            )
            _logger.info(
                "Deployed K8s sandbox %s for sandbox %s (model=%s)",
                svc_name,
                self.id,
                self.model_type,
            )
        except Exception as e:
            _logger.error("K8s sandbox deploy failed for sandbox %s: %s", self.id, e)
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": str(e)[:1000],
                }
            )
            return

        k8s_model = self.env["atlas.sandbox.k8s"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                status = k8s_model.get_sandbox_status(self)
                if status == "running":
                    self.write({"docker_status": "running"})
                    _logger.info(
                        "K8s sandbox %s is now running",
                        self.id,
                    )
                    return
                if status == "error":
                    self.write(
                        {
                            "docker_status": "error",
                            "docker_error": "K8s deployment failed",
                        }
                    )
                    return
            except Exception as e:
                _logger.debug("K8s readiness poll error: %s", e)
            time.sleep(5)

        _logger.warning(
            "K8s sandbox %s did not become ready within 300s, "
            "cron will continue reconciliation",
            self.id,
        )

    def _stop_local(self):
        if self.docker_status == "stopped":
            return

        compose_bin = _compose_cmd()
        project_name = self.docker_compose_project
        workdir = self.docker_workdir

        if compose_bin and project_name and workdir and os.path.isdir(workdir):
            try:
                cmd = compose_bin + ["-p", project_name]
                cmd += ["-f", "docker-compose.yml"]
                override = os.path.join(workdir, "docker-compose.override.yml")
                if os.path.isfile(override):
                    cmd += ["-f", "docker-compose.override.yml"]
                cmd += ["down", "--volumes", "--remove-orphans"]

                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    cwd=workdir,
                )
                _logger.info(
                    "Stopped sandbox (project=%s) sandbox=%s",
                    project_name,
                    self.id,
                )
            except Exception as e:
                _logger.warning(
                    "Failed to stop compose project %s: %s", project_name, e
                )
        elif compose_bin and project_name:
            try:
                subprocess.run(
                    compose_bin
                    + [
                        "-p",
                        project_name,
                        "down",
                        "--volumes",
                        "--remove-orphans",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except Exception as e:
                _logger.warning("Force stop failed: %s", e)

        if workdir and os.path.isdir(workdir):
            try:
                shutil.rmtree(workdir)
            except Exception as e:
                _logger.warning("Could not clean workdir %s: %s", workdir, e)

        self.write(
            {
                "docker_compose_project": False,
                "docker_status": "stopped",
                "docker_port": 0,
                "docker_litellm_port": 0,
                "docker_gateway_token": False,
                "docker_workdir": False,
                "docker_error": False,
            }
        )

    # ------------------------------------------------------------------
    # Helper methods for local lifecycle
    # ------------------------------------------------------------------

    def _prepare_workdir(
        self, gateway_token, gateway_port, litellm_port, db_port
    ):
        env = _load_dotenv()
        source_dir = _module_sandbox_dir()
        if not source_dir or not os.path.isdir(source_dir):
            raise UserError(
                "Bundled sandbox_docker directory not found in atlas module."
            )

        workdir = os.path.join(
            tempfile.gettempdir(),
            "atlas-sandbox",
            "atlas-%d-%s" % (self.atlas_id.id, self.model_type),
        )
        if os.path.exists(workdir):
            shutil.rmtree(workdir)
        os.makedirs(workdir)

        for filename in ("Dockerfile", "litellm-patch-entrypoint.sh"):
            src = os.path.join(source_dir, filename)
            dst = os.path.join(workdir, filename)
            if os.path.isfile(src):
                shutil.copy2(src, dst)

        src = os.path.join(source_dir, "docker-compose.yml")
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(workdir, "docker-compose.yml"))

        data_dir = os.path.join(workdir, "data", "default")
        os.makedirs(data_dir, exist_ok=True)
        ws_dir = os.path.join(data_dir, "workspace")
        os.makedirs(os.path.join(ws_dir, "memory"), exist_ok=True)
        os.makedirs(os.path.join(ws_dir, "skills"), exist_ok=True)

        aws_bearer = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        aws_region = env.get("AWS_REGION", "ap-south-1").strip()
        bedrock_arn = env.get("BEDROCK_MODEL_ARN", "").strip()
        litellm_key = env.get("LITELLM_MASTER_KEY", "").strip()
        if not litellm_key:
            litellm_key = "sk-atlas-%s" % secrets.token_hex(8)

        origins = [
            "http://localhost:18789",
            "http://127.0.0.1:18789",
            "http://0.0.0.0:18789",
            "http://localhost:8069",
            "http://127.0.0.1:8069",
        ]
        if gateway_port != 18789:
            origins.append("http://localhost:%d" % gateway_port)
            origins.append("http://127.0.0.1:%d" % gateway_port)

        config = {
            "gateway": {
                "bind": "lan",
                "auth": {"mode": "token", "token": gateway_token},
                "trustedProxies": [
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "10.0.0.0/8",
                ],
                "controlUi": {
                    "allowedOrigins": origins,
                    "dangerouslyDisableDeviceAuth": True,
                },
            },
            "browser": {
                "enabled": True,
                "headless": True,
                "noSandbox": True,
                "defaultProfile": "openclaw",
                "executablePath": "/usr/bin/chromium",
            },
            "models": {"providers": {}},
        }

        providers = config["models"]["providers"]

        providers["litellm"] = {
            "baseUrl": "http://litellm:4000/v1",
            "apiKey": litellm_key,
            "auth": "api-key",
            "api": "openai-completions",
            "models": [
                {
                    "id": "glm-5",
                    "name": "glm-5",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 131072,
                    "maxTokens": 32768,
                },
            ],
        }

        default_model = MODEL_DEFAULTS.get(self.model_type)
        if default_model:
            config["agents"] = {"defaults": {"model": default_model}}

        with open(os.path.join(data_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)

        kimi_arn = env.get("KIMI_BEDROCK_MODEL_ARN", "").strip()
        kimi_region = env.get("KIMI_AWS_REGION", "us-east-1").strip()
        glm_arn = env.get("GLM_BEDROCK_MODEL_ARN", "").strip()
        glm_region = env.get("GLM_AWS_REGION", "us-east-1").strip()
        litellm_yaml = _DEFAULT_LITELLM_CONFIG.format(
            bedrock_arn=bedrock_arn or "PLACEHOLDER",
            aws_region=aws_region,
            kimi_bedrock_arn=kimi_arn or "PLACEHOLDER",
            kimi_aws_region=kimi_region,
            glm_bedrock_arn=glm_arn or "PLACEHOLDER",
            glm_aws_region=glm_region,
        )
        with open(os.path.join(workdir, "litellm-config.yaml"), "w") as f:
            f.write(litellm_yaml)

        gog_config_dir = os.path.join(workdir, "gog-config")
        os.makedirs(os.path.join(gog_config_dir, "gogcli", "keyring"), exist_ok=True)
        gog_auth_raw = self.atlas_id.gog_auth
        gog_auth_token_raw = self.atlas_id.gog_auth_token
        _logger.info(
            "[GogAuth→Docker] task=%s gog_auth present=%s length=%s gog_auth_token present=%s length=%s",
            self.atlas_id.id,
            bool(gog_auth_raw),
            len(gog_auth_raw) if gog_auth_raw else 0,
            bool(gog_auth_token_raw),
            len(gog_auth_token_raw) if gog_auth_token_raw else 0,
        )

        if gog_auth_raw:
            try:
                gog_data = json.loads(gog_auth_raw)
                if isinstance(gog_data, dict):
                    client_secret_obj = None
                    if "client_secret" in gog_data and isinstance(
                        gog_data["client_secret"], dict
                    ):
                        client_secret_obj = gog_data["client_secret"]
                    elif "installed" in gog_data or "web" in gog_data:
                        client_secret_obj = gog_data

                    if client_secret_obj:
                        cs_path = os.path.join(
                            gog_config_dir, "gogcli", "client_secret.json"
                        )
                        with open(cs_path, "w") as f:
                            json.dump(client_secret_obj, f)
                        _logger.info(
                            "[GogAuth→Docker] wrote client_secret.json to %s", cs_path
                        )
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    "[GogAuth→Docker] Could not parse gog_auth JSON for task %s",
                    self.atlas_id.id,
                )

        if gog_auth_token_raw:
            try:
                token_data = json.loads(gog_auth_token_raw)
                if isinstance(token_data, dict):
                    gog_files = token_data.get("tokens", {})
                    written_files = []
                    for rel_path, content in gog_files.items():
                        if rel_path in ("client_secret", "tokens"):
                            continue
                        if not isinstance(content, str):
                            continue
                        abs_path = os.path.join(gog_config_dir, "gogcli", rel_path)
                        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                        with open(abs_path, "w") as f:
                            f.write(content)
                        written_files.append(rel_path)
                    _logger.info(
                        "[GogAuth→Docker] wrote %d token files to %s: %s",
                        len(written_files),
                        gog_config_dir,
                        written_files,
                    )
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    "[GogAuth→Docker] Could not parse gog_auth_token JSON for task %s",
                    self.atlas_id.id,
                )

        gog_cfg = os.path.join(gog_config_dir, "gogcli", "config.json")
        if not os.path.isfile(gog_cfg):
            with open(gog_cfg, "w") as f:
                json.dump({"keyring_backend": "file"}, f)

        nginx_conf = (
            "map $http_upgrade $connection_upgrade {\n"
            "    default upgrade;\n"
            "    ''      close;\n"
            "}\n"
            "server {\n"
            "    listen 80;\n"
            "    server_name _;\n"
            "    proxy_buffering off;\n"
            "    location /browser-api/ {\n"
            "        proxy_pass http://openclaw:18791/;\n"
            "        proxy_http_version 1.1;\n"
            '        proxy_set_header Authorization "Bearer %s";\n'
            "        proxy_set_header Host localhost;\n"
            "        proxy_read_timeout 30s;\n"
            "        proxy_send_timeout 30s;\n"
            "    }\n"
            "    location / {\n"
            "        proxy_pass http://openclaw:18789;\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Upgrade $http_upgrade;\n"
            "        proxy_set_header Connection $connection_upgrade;\n"
            "        proxy_set_header Host localhost;\n"
            "        proxy_set_header Origin $http_origin;\n"
            "        proxy_set_header User-Agent $http_user_agent;\n"
            "        proxy_hide_header X-Frame-Options;\n"
            "        proxy_hide_header Content-Security-Policy;\n"
            "        proxy_read_timeout 600s;\n"
            "        proxy_send_timeout 600s;\n"
            "    }\n"
            "}\n"
        ) % gateway_token
        with open(os.path.join(workdir, "nginx.conf"), "w") as f:
            f.write(nginx_conf)

        override = (
            "services:\n"
            "  openclaw:\n"
            '    entrypoint: ["node", "openclaw.mjs", "gateway",'
            ' "--allow-unconfigured", "--token", "%s"]\n'
            "    command: []\n"
            "    ports: !override []\n"
            "    volumes:\n"
            "      - ./data/${PERSONA:-default}:/home/node/.openclaw\n"
            "      - ./gog-config:/home/node/.config:rw\n"
            "    environment:\n"
            "      - GOG_KEYRING_PASSWORD=${GOG_KEYRING_PASSWORD:-}\n"
            "      - GOG_ACCOUNT=${GOG_ACCOUNT:-}\n"
            "  nginx:\n"
            "    image: nginx:alpine\n"
            "    depends_on:\n"
            "      - openclaw\n"
            "    volumes:\n"
            "      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro\n"
            "    ports:\n"
            '      - "%d:80"\n'
            "    networks:\n"
            "      - frontend\n"
            "  litellm:\n"
            "    ports:\n"
            '      - "%d:4000"\n'
            "  db:\n"
            "    ports:\n"
            '      - "%d:5432"\n'
        ) % (gateway_token, gateway_port, litellm_port, db_port)
        with open(os.path.join(workdir, "docker-compose.override.yml"), "w") as f:
            f.write(override)

        return workdir

    def _build_compose_env(self, gateway_token):
        self.ensure_one()

        env = _load_dotenv().copy()
        env["PERSONA"] = "default"
        env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token

        if not env.get("LITELLM_MASTER_KEY"):
            env["LITELLM_MASTER_KEY"] = "sk-atlas-%s" % secrets.token_hex(8)

        gog_kp = self.atlas_id.password or ""
        if gog_kp:
            env["GOG_KEYRING_PASSWORD"] = gog_kp

        task_email = self.atlas_id.email
        if task_email:
            env["GOG_ACCOUNT"] = task_email

        _logger.info(
            "[GogAuth→Docker] _build_compose_env task=%s GOG_ACCOUNT=%s GOG_KEYRING_PASSWORD=%s",
            self.atlas_id.id,
            task_email or "(none)",
            "***set***" if gog_kp else "(empty)",
        )
        return env

    def _wait_for_health(self, compose_bin, project_name, workdir):
        import urllib.request

        deadline = time.monotonic() + _HEALTH_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    compose_bin
                    + ["-p", project_name, "ps", "--format", "json", "openclaw"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    cwd=workdir,
                )
                output = result.stdout.strip()
                if output:
                    try:
                        data = json.loads(output)
                    except json.JSONDecodeError:
                        data = json.loads(output.splitlines()[0])

                    if isinstance(data, list):
                        data = data[0] if data else {}

                    state = (data.get("State") or "").lower()
                    if state in ("exited", "dead"):
                        _logger.warning(
                            "openclaw container exited (project=%s, state=%s)",
                            project_name,
                            state,
                        )
                        return False
            except (subprocess.TimeoutExpired, Exception) as e:
                _logger.debug("Health poll compose-ps error: %s", e)

            try:
                urllib.request.urlopen(
                    "http://localhost:%d/healthz" % self.docker_port,
                    timeout=5,
                )
                return True
            except Exception:
                pass

            time.sleep(_HEALTH_POLL_INTERVAL)

        return False

    def _capture_container_logs(self, compose_bin, project_name, workdir):
        try:
            result = subprocess.run(
                compose_bin + ["-p", project_name, "logs", "--tail", "30", "openclaw"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                cwd=workdir,
            )
            return result.stdout.strip() or result.stderr.strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Status reconciliation (local Docker)
    # ------------------------------------------------------------------

    def _check_local_status(self):
        """Check actual Docker container state and reconcile with DB status."""
        self.ensure_one()

        with _SANDBOX_LOCK:
            if self.id in _SANDBOX_STARTING:
                return

        if not self.docker_compose_project:
            if self.docker_status not in ("stopped",):
                self.write({"docker_status": "stopped"})
            return

        if self.docker_status == "running":
            return

        compose_bin = _compose_cmd()
        if not compose_bin:
            return

        workdir = self.docker_workdir
        try:
            cmd = compose_bin + [
                "-p",
                self.docker_compose_project,
                "ps",
                "--format",
                "json",
                "openclaw",
            ]
            kw = {
                "capture_output": True,
                "text": True,
                "timeout": 10,
                "check": False,
            }
            if workdir and os.path.isdir(workdir):
                kw["cwd"] = workdir
            result = subprocess.run(cmd, **kw)

            output = result.stdout.strip()
            if not output:
                if self.docker_status == "starting":
                    return
                if self.docker_status != "stopped":
                    _logger.info(
                        "[StatusCheck] No container found for project=%s sandbox=%s, marking stopped",
                        self.docker_compose_project,
                        self.id,
                    )
                    self.write(
                        {
                            "docker_status": "stopped",
                            "docker_compose_project": False,
                            "docker_port": 0,
                            "docker_litellm_port": 0,
                            "docker_gateway_token": False,
                            "docker_workdir": False,
                            "docker_error": False,
                        }
                    )
                return

            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                data = json.loads(output.splitlines()[0])

            if isinstance(data, list):
                data = data[0] if data else {}

            state = (data.get("State") or "").lower()
            health = (data.get("Health") or "").lower()

            if state in ("exited", "dead"):
                if self.docker_status != "error":
                    _logger.info(
                        "[StatusCheck] Container exited for project=%s sandbox=%s (state=%s), marking error",
                        self.docker_compose_project,
                        self.id,
                        state,
                    )
                    self.write(
                        {
                            "docker_status": "error",
                            "docker_error": "Container exited unexpectedly (state=%s)"
                            % state,
                        }
                    )
            elif state == "running" and health == "unhealthy":
                if self.docker_status == "starting":
                    _logger.debug(
                        "[StatusCheck] Container unhealthy during startup for project=%s sandbox=%s, "
                        "waiting for health check to pass",
                        self.docker_compose_project,
                        self.id,
                    )
                elif self.docker_status != "error":
                    _logger.info(
                        "[StatusCheck] Container unhealthy for project=%s sandbox=%s, marking error",
                        self.docker_compose_project,
                        self.id,
                    )
                    self.write(
                        {
                            "docker_status": "error",
                            "docker_error": "Container running but unhealthy",
                        }
                    )
            elif state == "running" and health in ("", "healthy"):
                if self.docker_status != "running":
                    _logger.info(
                        "[StatusCheck] Container running for project=%s sandbox=%s, updating to running",
                        self.docker_compose_project,
                        self.id,
                    )
                    self.write({"docker_status": "running"})
            elif state == "running" and health == "starting":
                _logger.debug(
                    "[StatusCheck] Container running, health starting for project=%s sandbox=%s",
                    self.docker_compose_project,
                    self.id,
                )

        except subprocess.TimeoutExpired:
            _logger.debug(
                "[StatusCheck] Timed out checking status for sandbox %s", self.id
            )
        except Exception as e:
            _logger.debug(
                "[StatusCheck] Error checking status for sandbox %s: %s", self.id, e
            )

    def action_check_status(self):
        """Public action: reconcile DB docker_status with actual Docker state."""
        mode = self._deployment_mode()
        result = {}
        for sandbox in self:
            if (
                sandbox.docker_status in ("stopped",)
                and not sandbox.docker_compose_project
            ):
                result[sandbox.id] = sandbox.docker_status
                continue

            if mode == "local":
                sandbox._check_local_status()

            result[sandbox.id] = sandbox.docker_status
        return result

    # ------------------------------------------------------------------
    # Cron reconciliation (k8s)
    # ------------------------------------------------------------------

    @api.model
    def _cron_reconcile(self):
        mode = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("atlas.deployment_mode", "local")
            .strip()
        )
        if mode != "k8s":
            return

        sandboxes = self.sudo().search(
            [("docker_status", "in", ["starting", "running"])]
        )
        if not sandboxes:
            return

        k8s = self.env["atlas.sandbox.k8s"]
        for sandbox in sandboxes:
            try:
                status = k8s.get_sandbox_status(sandbox)
                if status != sandbox.docker_status:
                    sandbox.write({"docker_status": status})
                    if status == "error":
                        sandbox.write(
                            {
                                "docker_error": "Sandbox deployment not found after timeout",
                            }
                        )
                    if status in ("running", "error"):
                        partner = (
                            sandbox.employee_id.user_id.partner_id
                            or sandbox.atlas_id.user_id.partner_id
                        )
                        if partner:
                            self.env["bus.bus"]._sendone(
                                partner,
                                "atlas/sandbox_ready",
                                {
                                    "sandbox_id": sandbox.id,
                                    "docker_status": status,
                                    "error": sandbox.docker_error or "",
                                    "model_type": sandbox.model_type,
                                },
                            )
            except Exception as e:
                _logger.error(
                    "Reconciliation error for sandbox %s: %s",
                    sandbox.id,
                    e,
                )
