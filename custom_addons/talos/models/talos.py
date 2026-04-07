import logging
import os
import secrets
import subprocess
import time

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

GATEWAY_PORT = 18789
LITELLM_PORT = 4000

# Maximum time (seconds) to wait for the gateway health-check after compose up.
_HEALTH_WAIT_TIMEOUT = 120
_HEALTH_POLL_INTERVAL = 3


def _docker_available():
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _compose_cmd():
    for cmd in (["docker", "compose"], ["docker-compose"]):
        try:
            result = subprocess.run(
                cmd + ["version"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return cmd
        except FileNotFoundError:
            continue
    return None


class Talos(models.Model):
    _name = "talos.talos"
    _description = "Talos"

    task_id = fields.Char(string="Task ID", readonly=True, copy=False)
    parsona = fields.Many2one("talos.domain", string="Parsona")
    task_status = fields.Selection(
        [("Submitted", "Submitted"), ("NotSubmitted", "Not Submitted")]
    )
    employee_id = fields.Many2one("hr.employee")
    user_id = fields.Many2one(related="employee_id.user_id")
    turn_ids = fields.One2many("talos.turn", "talos_id", string="Turns")

    docker_persona = fields.Char(string="Persona", default="marcus")
    docker_compose_project = fields.Char(
        string="Compose Project", readonly=True, copy=False
    )
    docker_status = fields.Selection(
        [
            ("stopped", "Stopped"),
            ("starting", "Starting"),
            ("running", "Running"),
            ("error", "Error"),
        ],
        string="Docker Status",
        default="stopped",
        readonly=True,
    )
    docker_port = fields.Integer(string="Gateway Port", readonly=True)
    docker_litellm_port = fields.Integer(string="LiteLLM Port", readonly=True)
    docker_gateway_token = fields.Char(
        string="Gateway Token", readonly=True, copy=False
    )
    docker_dashboard_url = fields.Char(
        string="Dashboard URL", compute="_compute_dashboard_url"
    )
    docker_error = fields.Text(string="Docker Error", readonly=True)

    def _deployment_mode(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("talos.deployment_mode", "local")
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
                svc_name = "talos-sandbox-%s" % rec.id
                rec.docker_dashboard_url = (
                    "http://%s.ethara.svc.cluster.local:18789/#token=%s"
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

    def action_start_sandbox(self):
        self.ensure_one()
        mode = self._deployment_mode()

        if mode == "k8s":
            self._start_k8s()
        else:
            self._start_local()

    def action_stop_sandbox(self):
        self.ensure_one()
        mode = self._deployment_mode()

        if mode == "k8s":
            self._stop_k8s()
        else:
            self._stop_local()

    @api.model
    def _cron_reconcile_sandboxes(self):
        mode = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("talos.deployment_mode", "local")
            .strip()
        )
        if mode == "k8s":
            self.env["talos.sandbox.k8s"].reconcile_sandboxes()

    def _start_k8s(self):
        if self.docker_status == "running":
            raise UserError("Sandbox is already running for this task.")

        gateway_token = secrets.token_hex(32)
        self.write(
            {
                "docker_status": "starting",
                "docker_gateway_token": gateway_token,
                "docker_error": False,
            }
        )

        try:
            self.env["talos.sandbox.k8s"].deploy_sandbox(self)
            svc_name = "talos-sandbox-%s" % self.id
            self.write(
                {
                    "docker_compose_project": svc_name,
                    "docker_status": "starting",
                    "docker_port": 18789,
                }
            )
            _logger.info(
                "Deployed K8s sandbox %s for task %s (persona=%s)",
                svc_name,
                self.id,
                self.docker_persona or "marcus",
            )
        except Exception as e:
            _logger.error("K8s sandbox deploy failed for task %s: %s", self.id, e)
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": str(e)[:1000],
                }
            )

    def _stop_k8s(self):
        if self.docker_status == "stopped":
            return

        try:
            self.env["talos.sandbox.k8s"].destroy_sandbox(self)
            _logger.info("Destroyed K8s sandbox for task %s", self.id)
        except Exception as e:
            _logger.warning("K8s sandbox destroy failed for task %s: %s", self.id, e)

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

    def _get_sandbox_dir(self):
        ICP = self.env["ir.config_parameter"].sudo()
        sandbox_dir = ICP.get_param("talos.sandbox_dir", "").strip()
        if not sandbox_dir:
            raise UserError(
                "Sandbox directory not configured. "
                "Go to Settings > Talos and set the Sandbox Docker Directory."
            )
        docker_dir = os.path.join(sandbox_dir, "docker")
        if not os.path.isdir(docker_dir):
            raise UserError("Sandbox docker directory not found: %s" % docker_dir)
        return sandbox_dir

    def _build_compose_env(self, sandbox_dir, gateway_token):
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        persona = self.docker_persona or "marcus"

        env = os.environ.copy()
        env["PERSONA"] = persona
        env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token

        aws_bearer = ICP.get_param("talos.aws_bearer_token", "").strip()
        if aws_bearer:
            env["AWS_BEARER_TOKEN_BEDROCK"] = aws_bearer

        aws_region = ICP.get_param("talos.aws_region", "ap-south-1").strip()
        env["AWS_REGION"] = aws_region

        bedrock_arn = ICP.get_param("talos.bedrock_model_arn", "").strip()
        if bedrock_arn:
            env["BEDROCK_MODEL_ARN"] = bedrock_arn

        litellm_key = ICP.get_param("talos.litellm_master_key", "").strip()
        if litellm_key:
            env["LITELLM_MASTER_KEY"] = litellm_key
        else:
            env["LITELLM_MASTER_KEY"] = "sk-talos-%s" % secrets.token_hex(8)

        litellm_db_pw = ICP.get_param("talos.litellm_db_password", "").strip()
        if litellm_db_pw:
            env["LITELLM_DB_PASSWORD"] = litellm_db_pw

        return env

    def _ensure_data_dir(self, docker_dir, persona):
        """Create the per-persona data directory if it doesn't exist.

        The base docker-compose.yml bind-mounts ``./data/{persona}`` into the
        container.  If the directory is missing Docker will create it as
        root-owned, which causes permission errors inside the container.
        """
        data_dir = os.path.join(docker_dir, "data", persona)
        os.makedirs(data_dir, exist_ok=True)

    def _reset_stale_config(self, docker_dir, persona):
        """Remove the previous openclaw.json so the entrypoint writes a fresh one.

        The ``openclaw config set`` command can hang indefinitely when it tries
        to update a config file that contains a stale gateway token from a
        prior run.  Deleting the file forces the entrypoint to write config
        from scratch, which is always the desired behavior for ephemeral
        sandbox containers.
        """
        data_dir = os.path.join(docker_dir, "data", persona)
        for name in ("openclaw.json",):
            path = os.path.join(data_dir, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    _logger.debug("Removed stale config: %s", path)
                except OSError as e:
                    _logger.warning("Could not remove stale config %s: %s", path, e)

    def _wait_for_health(self, compose_bin, project_name, docker_dir):
        """Poll ``docker compose ps`` until the openclaw service is healthy.

        Returns ``True`` when the gateway is healthy, ``False`` on timeout or
        if the container exited.
        """
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
                    cwd=docker_dir,
                )
                output = result.stdout.strip()
                if not output:
                    time.sleep(_HEALTH_POLL_INTERVAL)
                    continue

                # compose v2 may return one JSON object per line or a JSON array
                import json

                try:
                    data = json.loads(output)
                except json.JSONDecodeError:
                    # multiple JSON objects (one per line) — take the first
                    data = json.loads(output.splitlines()[0])

                if isinstance(data, list):
                    data = data[0] if data else {}

                state = (data.get("State") or "").lower()
                health = (data.get("Health") or "").lower()

                if state == "exited" or state == "dead":
                    _logger.warning(
                        "openclaw container exited during health wait "
                        "(project=%s, state=%s)",
                        project_name,
                        state,
                    )
                    return False

                if health == "healthy" or state == "running":
                    # Also probe the gateway directly to be sure
                    try:
                        import urllib.request

                        urllib.request.urlopen(
                            "http://localhost:%d/healthz" % self.docker_port,
                            timeout=5,
                        )
                        return True
                    except Exception:
                        # Container running but healthz not responding yet
                        pass

            except (subprocess.TimeoutExpired, Exception) as e:
                _logger.debug("Health poll error: %s", e)

            time.sleep(_HEALTH_POLL_INTERVAL)

        return False

    def _capture_container_logs(self, compose_bin, project_name, docker_dir):
        """Capture the last few lines of openclaw container logs."""
        try:
            result = subprocess.run(
                compose_bin + ["-p", project_name, "logs", "--tail", "30", "openclaw"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                cwd=docker_dir,
            )
            return result.stdout.strip() or result.stderr.strip()
        except Exception:
            return ""

    def _start_local(self):
        if not self.env.user.has_group("talos.group_talos_admin"):
            raise UserError("Local mode is restricted to Talos administrators.")

        if self.docker_status == "running" and self.docker_compose_project:
            raise UserError("Docker stack is already running for this task.")

        if not _docker_available():
            raise UserError(
                "Docker is not available on this server. "
                "Please ensure the Docker daemon is running."
            )

        compose_bin = _compose_cmd()
        if not compose_bin:
            raise UserError("docker compose (or docker-compose) not found.")

        sandbox_dir = self._get_sandbox_dir()
        docker_dir = os.path.join(sandbox_dir, "docker")
        gateway_token = secrets.token_hex(32)
        project_name = "talos-%d" % self.id
        persona = self.docker_persona or "marcus"

        persona_dir = os.path.join(sandbox_dir, "personas", persona)
        if not os.path.isdir(persona_dir):
            available = []
            personas_root = os.path.join(sandbox_dir, "personas")
            if os.path.isdir(personas_root):
                available = sorted(
                    d
                    for d in os.listdir(personas_root)
                    if os.path.isdir(os.path.join(personas_root, d))
                    and not d.startswith(".")
                )
            raise UserError(
                "Persona directory not found: %s\n"
                "Available personas: %s"
                % (persona_dir, ", ".join(available) or "(none)")
            )

        self.write({"docker_status": "starting", "docker_error": False})

        try:
            self._ensure_data_dir(docker_dir, persona)
        except Exception as e:
            _logger.warning("Could not pre-create data dir: %s", e)

        self._reset_stale_config(docker_dir, persona)

        compose_env = self._build_compose_env(sandbox_dir, gateway_token)

        cmd = compose_bin + [
            "-f",
            "docker-compose.yml",
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
                cwd=docker_dir,
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
                    "docker_status": "starting",
                    "docker_port": GATEWAY_PORT,
                    "docker_litellm_port": LITELLM_PORT,
                    "docker_gateway_token": gateway_token,
                    "docker_error": False,
                }
            )

            # Wait for the gateway health-check before declaring "running".
            healthy = self._wait_for_health(compose_bin, project_name, docker_dir)

            if healthy:
                self.write({"docker_status": "running"})
                _logger.info(
                    "Started local sandbox stack (project=%s) for task %s — "
                    "gateway=:%d litellm=:%d persona=%s",
                    project_name,
                    self.id,
                    GATEWAY_PORT,
                    LITELLM_PORT,
                    persona,
                )
            else:
                logs = self._capture_container_logs(
                    compose_bin, project_name, docker_dir
                )
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
                    "Gateway health-check failed for project %s (task %s)",
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
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": str(e)[:500],
                }
            )

    def _stop_local(self):
        if self.docker_status == "stopped":
            return

        compose_bin = _compose_cmd()
        project_name = self.docker_compose_project

        if compose_bin and project_name:
            try:
                sandbox_dir = self._get_sandbox_dir()
                docker_dir = os.path.join(sandbox_dir, "docker")

                cmd = compose_bin + ["-p", project_name]
                cmd += ["-f", "docker-compose.yml"]
                cmd += ["down", "--volumes", "--remove-orphans"]

                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    cwd=docker_dir,
                )
                _logger.info(
                    "Stopped local sandbox stack (project=%s) for task %s",
                    project_name,
                    self.id,
                )
            except UserError:
                _logger.warning(
                    "Sandbox dir not configured, attempting force stop of project %s",
                    project_name,
                )
                try:
                    subprocess.run(
                        (compose_bin or ["docker", "compose"])
                        + ["-p", project_name, "down", "--volumes", "--remove-orphans"],
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                except Exception as e:
                    _logger.warning("Force stop failed: %s", e)
            except Exception as e:
                _logger.warning(
                    "Failed to stop compose project %s: %s", project_name, e
                )

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


class TalosTurn(models.Model):
    _name = "talos.turn"
    _description = "Talos Turn"

    talos_id = fields.Many2one("talos.talos", string="Talos")
    turn_number = fields.Integer(string="Turn Number")
    turn_status = fields.Selection([("Pending", "Pending"), ("Completed", "Completed")])
    prompt = fields.Text(string="Prompt")
