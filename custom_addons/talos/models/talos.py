<<<<<<< Updated upstream
from odoo import models, fields, api


class Talos(models.Model):
    _name = 'talos.talos'
    _description = 'Talos'

    task_id = fields.Char(string="Task ID", readonly=True, copy=False)
    parsona = fields.Many2one('talos.domain', string='Parsona')
    task_status = fields.Selection([('Submitted', 'Submitted'), ('NotSubmitted', 'Not Submitted')])
    employee_id = fields.Many2one('hr.employee')
    user_id = fields.Many2one(related='employee_id.user_id')
    turn_ids = fields.One2many('talos.turn', 'talos_id', string='Turns')

class TalosTurn(models.Model):
    _name = 'talos.turn'
    _description = 'Talos Turn'

    talos_id = fields.Many2one('talos.talos', string='Talos')
    turn_number = fields.Integer(string='Turn Number')
    turn_status = fields.Selection([('Pending', 'Pending'), ('Completed', 'Completed')])
    prompt = fields.Text(string='Prompt')
=======
import logging
import os
import secrets
import subprocess

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

GATEWAY_PORT_BASE = 19000
LITELLM_PORT_BASE = 14000
DB_PORT_BASE = 15432


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

    def _allocate_ports(self):
        self.ensure_one()
        offset = self.id % 1000
        return (
            GATEWAY_PORT_BASE + offset,
            LITELLM_PORT_BASE + offset,
            DB_PORT_BASE + offset,
        )

    def _build_compose_env(self, sandbox_dir, gateway_port, gateway_token):
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

    def _write_port_override(self, project_dir, gateway_port, litellm_port, db_port):
        override_path = os.path.join(project_dir, "docker-compose.override.yml")
        content = (
            "services:\n"
            "  openclaw:\n"
            "    ports:\n"
            '      - "%d:18789"\n'
            "  litellm:\n"
            "    ports:\n"
            '      - "%d:4000"\n'
            "  db:\n"
            "    ports:\n"
            '      - "%d:5432"\n'
        ) % (gateway_port, litellm_port, db_port)

        with open(override_path, "w") as f:
            f.write(content)
        return override_path

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
        gateway_port, litellm_port, db_port = self._allocate_ports()
        gateway_token = secrets.token_hex(32)
        project_name = "talos-%d" % self.id

        self.write({"docker_status": "starting", "docker_error": False})

        try:
            override_path = self._write_port_override(
                docker_dir, gateway_port, litellm_port, db_port
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "Failed to write port override: %s" % str(e)[:500],
                }
            )
            return

        compose_env = self._build_compose_env(sandbox_dir, gateway_port, gateway_token)

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
                    "docker_status": "running",
                    "docker_port": gateway_port,
                    "docker_litellm_port": litellm_port,
                    "docker_gateway_token": gateway_token,
                    "docker_error": False,
                }
            )
            _logger.info(
                "Started local sandbox stack (project=%s) for task %s — "
                "gateway=:%d litellm=:%d persona=%s",
                project_name,
                self.id,
                gateway_port,
                litellm_port,
                self.docker_persona or "marcus",
            )

        except subprocess.TimeoutExpired:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "docker compose up timed out after 300 seconds",
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
                if os.path.isfile(
                    os.path.join(docker_dir, "docker-compose.override.yml")
                ):
                    cmd += ["-f", "docker-compose.override.yml"]
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

            override_path = None
            try:
                sandbox_dir = self._get_sandbox_dir()
                override_path = os.path.join(
                    sandbox_dir, "docker", "docker-compose.override.yml"
                )
            except UserError:
                pass
            if override_path and os.path.isfile(override_path):
                try:
                    os.remove(override_path)
                except OSError:
                    pass

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
>>>>>>> Stashed changes
