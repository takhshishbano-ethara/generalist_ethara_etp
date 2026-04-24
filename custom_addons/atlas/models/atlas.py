import json
import logging
import os
import subprocess

from odoo import models, fields, api
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)

GATEWAY_PORT_BASE = 19000
LITELLM_PORT_BASE = 14000
DB_PORT_BASE = 15432

_HEALTH_WAIT_TIMEOUT = 1200
_HEALTH_POLL_INTERVAL = 3

_prompt_cache = {}


def _get_prompt(filename):
    mod_path = get_module_path("atlas")
    if not mod_path:
        return ""
    for path in (
        os.path.join(mod_path, "prompts", filename),
        os.path.join(mod_path, filename),
    ):
        if os.path.isfile(path):
            mtime = os.path.getmtime(path)
            cached = _prompt_cache.get(filename)
            if cached and cached[1] == mtime:
                return cached[0]
            with open(path, "r") as f:
                text = f.read().strip()
            _prompt_cache[filename] = (text, mtime)
            return text
    return ""


import re as _re


def _is_degenerate_output(text):
    if not text or len(text) < 20:
        return True
    repeated = _re.search(r"(.)\1{15,}", text)
    if repeated:
        return True
    unique_chars = len(set(text.lower()))
    if unique_chars < 8 and len(text) > 30:
        return True
    return False


def generate_description_from_turns(env, turns):
    if not turns:
        return "", {}

    system_prompt = _get_prompt("description_prompt.md") or _get_prompt("task_description_prompt.md")
    if not system_prompt:
        _logger.warning("generate_description_from_turns: no description_prompt.md")
        return "", {}

    dotenv = _load_dotenv()
    api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
    ICP = env["ir.config_parameter"].sudo()
    inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()
    region = (ICP.get_param("atlas.bedrock_region") or "ap-south-1").strip()

    _logger.info(
        "generate_description_from_turns: api_key_set=%s arn=%s region=%s",
        bool(api_key), inference_arn[:60] if inference_arn else "(empty)", region,
    )

    if not api_key or not inference_arn:
        _logger.warning(
            "generate_description_from_turns: missing credentials — "
            "api_key_set=%s inference_arn='%s' region='%s'",
            bool(api_key), inference_arn or "(empty)", region,
        )
        return "", {}

    seed_prompt = ""
    prompts = []
    for t in turns:
        if t.prompt and not t.is_hint_turn and (t.response or t.turn_status == "Completed"):
            if not seed_prompt:
                seed_prompt = t.prompt.strip()
            prompts.append(t.prompt.strip())

    if not prompts:
        _logger.warning("generate_description_from_turns: no sent prompts found")
        return "", {}

    import json as _json
    messages_text = _json.dumps(
        [{"role": "user", "content": p} for p in prompts],
        ensure_ascii=False,
    )[:16000]
    user_message = "## Seed Prompt\n%s\n\n## User Prompts\n%s" % (
        seed_prompt,
        messages_text,
    )

    try:
        from ..controllers.llm_assisst_qc import _call_bedrock_converse
        import time as _time

        _logger.info(
            "task_desc: calling Kimi arn=%s region=%s prompt_len=%d",
            inference_arn, region, len(user_message),
        )
        t0 = _time.monotonic()
        response_text, usage = _call_bedrock_converse(
            api_key=api_key,
            inference_arn=inference_arn,
            region=region,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=1024,
            temperature=0.3,
            timeout=90.0,
        )
        elapsed = _time.monotonic() - t0
        _logger.info(
            "task_desc: Kimi response elapsed=%.2fs in_tokens=%d out_tokens=%d "
            "response_len=%d raw_output=%.500s",
            elapsed,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            len(response_text),
            response_text,
        )
        desc = response_text.strip().replace("\n", " ")

        if _is_degenerate_output(desc):
            _logger.warning(
                "generate_description_from_turns: degenerate output (%d chars), discarding",
                len(desc),
            )
            return "", usage

        _logger.info(
            "generate_description_from_turns: generated %d chars, tokens=%s",
            len(desc), usage,
        )
        return desc, usage
    except Exception as e:
        _logger.exception("generate_description_from_turns failed")
        return "", {"error": str(e)}


def _parse_rubric_table(text):
    import re as _re

    code_block = _re.search(r"```(?:markdown)?\s*\n?(.*?)```", text, _re.DOTALL)
    parse_text = code_block.group(1) if code_block else text

    _QC_JUNK_PATTERNS = (
        "\u2713", "\u2717", "\u2714", "\u2716", "check ", "verdict",
        "self-qc", "qc note", "verification", "weakest",
    )

    def _is_separator(line):
        stripped = line.strip().strip("|").strip()
        return bool(_re.match(r"^[\s\-:| ]+$", stripped))

    def _is_header(line):
        lower = line.lower()
        return "criterion" in lower or "criteria" in lower or "category" in lower or "importance" in lower

    def _is_qc_junk(line):
        inner = line.strip().strip("|").strip().lower()
        return any(inner.startswith(p) for p in _QC_JUNK_PATTERNS)

    table_lines = []
    for line in parse_text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if _is_separator(stripped):
            continue
        if _is_header(stripped):
            continue
        if _is_qc_junk(stripped):
            continue
        table_lines.append(stripped)

    if not table_lines:
        _logger.warning("_parse_rubric_table: no table rows found in %d chars", len(text))
        return []

    _logger.info("_parse_rubric_table: found %d table lines", len(table_lines))

    VALID_IMPS = {
        "critically_detrimental", "detrimental", "slightly_detrimental",
        "slightly_important", "important", "critically_important",
    }
    VALID_CATS = {
        "factuality_hallucination", "task_completion", "instruction_following",
        "communication_style", "other",
    }

    criteria = []
    for line in table_lines:
        cols = [c.strip() for c in line.split("|")]
        cols = [c for c in cols if c]

        if len(cols) < 2:
            continue

        first_col = cols[0].strip()
        has_row_num = bool(_re.match(r"^[NC]?#?\d+\.?$", first_col))

        if has_row_num:
            data_cols = cols[1:]
        else:
            data_cols = cols

        criterion_text = ""
        for dc in data_cols:
            dc_stripped = dc.strip()
            if len(dc_stripped) > 15 and not _re.match(r"^\d+\s*:", dc_stripped):
                candidate_lower = dc_stripped.lower()
                if candidate_lower not in VALID_CATS and candidate_lower.replace(" ", "_") not in VALID_IMPS:
                    if not any(dc_stripped.startswith(p) for p in ("\u2713", "\u2717", "\u2714", "\u2716")):
                        criterion_text = dc_stripped
                        break

        if not criterion_text:
            if data_cols:
                criterion_text = data_cols[0].strip()

        if not criterion_text or len(criterion_text) < 10:
            continue
        if criterion_text.startswith("---"):
            continue
        if criterion_text.startswith(("\u2713", "\u2717", "\u2714", "\u2716")):
            continue
        if any(kw in criterion_text.lower() for kw in ("self-qc", "qc note", "verification check", "weakest field", "maxraw", "score =")):
            continue

        full_line = line
        category = "other"
        custom_category = ""
        for dc in data_cols:
            dc_lower = dc.strip().lower().replace(" ", "_")
            if dc_lower in VALID_CATS:
                category = dc_lower
                break
            elif dc.strip().lower().startswith("other:"):
                category = "other"
                custom_category = dc.strip().split(":", 1)[1].strip()
                break
            else:
                for vc in VALID_CATS:
                    if vc.replace("_", "") in dc_lower.replace("_", ""):
                        category = vc
                        break
                if category != "other":
                    break

        importance = "important"
        for dc in data_cols:
            dc_lower = dc.strip().lower().replace(" ", "_")
            if dc_lower in VALID_IMPS:
                importance = dc_lower
                break
            for vi in VALID_IMPS:
                if vi.replace("_", "") in dc_lower.replace("_", ""):
                    importance = vi
                    break
            if importance != "important":
                break

        is_negative = "\u274c" in full_line

        levels = []
        for col in cols:
            level_matches = _re.findall(r"(\d+)\s*:\s*([^|]+?)(?=\s*\d+\s*:|$)", col)
            if level_matches:
                for score_str, label in level_matches:
                    label = label.strip().rstrip("|").strip()
                    if label.startswith("\u2014"):
                        label = label[1:].strip()
                    levels.append({"score": int(score_str), "label": label})

        if not levels:
            levels = [
                {"score": 0, "label": ""},
                {"score": 1, "label": ""},
            ]

        suggestion = ""
        if len(data_cols) >= 2:
            last_col = data_cols[-1].strip()
            if last_col and not _re.match(r"^\d+\s*:", last_col) and "\u2705" not in last_col and "\u274c" not in last_col:
                if last_col.lower() not in VALID_CATS and last_col.lower().replace(" ", "_") not in VALID_IMPS:
                    if last_col != criterion_text:
                        suggestion = last_col

        criteria.append({
            "name": criterion_text,
            "category": category,
            "custom_category": custom_category,
            "importance": importance,
            "weight": max(lv["score"] for lv in levels) if levels else 2,
            "is_negative": is_negative,
            "suggestion": suggestion,
            "levels": levels,
        })

    _logger.info("_parse_rubric_table: parsed %d criteria", len(criteria))
    return criteria


def generate_rubric_from_turns(env, turns, task_id=None):
    if not turns:
        return [], {}

    system_prompt = _get_prompt("rubric_prompt.md")
    if not system_prompt:
        _logger.warning("generate_rubric_from_turns: no rubric_prompt.md")
        return [], {}

    dotenv = _load_dotenv()
    api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
    ICP = env["ir.config_parameter"].sudo()
    inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()
    region = (ICP.get_param("atlas.bedrock_region") or "ap-south-1").strip()

    if not api_key or not inference_arn:
        return [], {}

    sent_turns = [t for t in turns if t.prompt and not t.is_hint_turn and (t.response or t.turn_status == "Completed")]
    if not sent_turns:
        _logger.warning("generate_rubric_from_turns: no sent turns found (total turns=%d)", len(turns) if hasattr(turns, '__len__') else -1)
        return [], {}

    conversation_parts = []
    for t in sent_turns:
        conversation_parts.append("User: %s" % t.prompt.strip()[:800])
        if t.response:
            conversation_parts.append("Assistant: %s" % t.response.strip()[:800])

    goal = ""
    if task_id:
        task_rec = env["atlas.atlas"].browse(task_id)
        if task_rec.exists():
            goal = task_rec.goal_description or ""

    user_message = "## Goal\n%s\n\n## Conversation\n%s" % (
        goal if goal else "(Goal not yet generated — infer from conversation)",
        "\n".join(conversation_parts),
    )

    try:
        from ..controllers.llm_assisst_qc import _call_bedrock_converse

        response_text, usage = _call_bedrock_converse(
            api_key=api_key,
            inference_arn=inference_arn,
            region=region,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=4096,
            temperature=0.3,
            timeout=120.0,
        )
        _logger.info("generate_rubric_from_turns: response_len=%d tokens=%s", len(response_text), usage)
        _logger.info("generate_rubric_from_turns: first 500 chars: %s", response_text[:500])

        criteria = _parse_rubric_table(response_text)

        if not criteria:
            _logger.warning("generate_rubric_from_turns: table parse returned empty, trying JSON fallback")
            import json as _json
            text = response_text.strip()
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                text = text[start:end]
            try:
                criteria = _json.loads(text)
                if not isinstance(criteria, list):
                    criteria = []
                else:
                    _logger.info("generate_rubric_from_turns: JSON fallback parsed %d criteria", len(criteria))
            except (ValueError, _json.JSONDecodeError):
                _logger.warning("generate_rubric_from_turns: JSON fallback also failed")
                criteria = []

        return criteria, usage
    except Exception as e:
        _logger.warning("generate_rubric_from_turns failed: %s", e)
        return [], {}


def _load_dotenv():
    env = os.environ.copy()

    root = None
    conf_path = odoo_config.rcfile
    if conf_path:
        root = os.path.dirname(os.path.abspath(conf_path))
    if not root:
        root = os.getcwd()

    dotenv_path = os.path.join(root, ".env")
    if os.path.isfile(dotenv_path):
        with open(dotenv_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                env[key] = value
        _logger.debug("Loaded .env from %s", dotenv_path)

    return env


_DEFAULT_LITELLM_CONFIG = """\
model_list:
  - model_name: glm-5
    litellm_params:
      model: bedrock/converse/{glm_bedrock_arn}
      aws_region_name: {glm_aws_region}
      input_cost_per_token: 0.0000006
      output_cost_per_token: 0.000003

  - model_name: kimi-k2.5
    litellm_params:
      model: bedrock/converse/{kimi_bedrock_arn}
      aws_region_name: {kimi_aws_region}
      input_cost_per_token: 0.0000006
      output_cost_per_token: 0.000003

  - model_name: quiet_sand
    litellm_params:
      model: openai/quiet_sand
      api_base: https://api.llama.com/v1alpha
      api_key: os.environ/LLAMA_API_KEY

litellm_settings:
  drop_params: true
  modify_params: true
  telemetry: false
  num_retries: 1
  request_timeout: 900
  stream_timeout: 60

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL
  store_model_in_db: true
"""


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


def _module_sandbox_dir():
    mod_path = get_module_path("atlas")
    if not mod_path:
        return None
    return os.path.join(mod_path, "sandbox_docker")


class Atlas(models.Model):
    _name = "atlas.atlas"
    _description = "Atlas"

    is_atlas_admin = fields.Boolean(
        compute="_compute_is_atlas_admin",
        search="_search_is_atlas_admin",
    )

    @api.depends_context("uid")
    def _compute_is_atlas_admin(self):
        is_admin = self.env.user.has_group("etp_user_roles.group_quality_lead")
        for rec in self:
            rec.is_atlas_admin = is_admin

    def _search_is_atlas_admin(self, operator, value):
        if operator not in ("=", "!="):
            raise ValueError("Unsupported operator")
        is_admin = self.env.user.has_group("etp_user_roles.group_quality_lead")
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [] if is_admin else [("id", "=", False)]
        return [("id", "=", False)] if is_admin else []

    task_status = fields.Selection(
        [("Submitted", "Submitted"), ("NotSubmitted", "Not Submitted")]
    )
    employee_id = fields.Many2one(
        "hr.employee",
        default=lambda self: self.env.user.employee_id,
    )
    user_id = fields.Many2one(related="employee_id.user_id")

    email = fields.Char(string="Email")
    password = fields.Char(string="Password")
    gog_auth = fields.Text(string="Google Auth")
    gog_auth_token = fields.Text(string="Google Auth Token")

    sandbox_ids = fields.One2many("atlas.sandbox", "atlas_id", string="Sandboxes")
    qc_status = fields.Selection(
        [("pending", "Pending"), ("passed", "Passed"), ("failed", "Failed")],
        default="pending",
    )

    glm_sandbox_id = fields.Many2one(
        "atlas.sandbox", compute="_compute_sandbox_ids", string="GLM Sandbox"
    )

    glm_status = fields.Selection(related="glm_sandbox_id.docker_status")
    glm_session_status = fields.Selection(related="glm_sandbox_id.session_status")

    goal_description = fields.Text(string="Goal Description")
    rubric_criterion_ids = fields.One2many(
        "atlas.rubric.criterion", "atlas_id", string="Rubric Criteria"
    )

    goal_generation_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("running", "Running"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Goal Generation Status",
        default="idle",
    )
    rubric_generation_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("running", "Running"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Rubric Generation Status",
        default="idle",
    )

    glm_input_tokens = fields.Integer(string="GLM Input Tokens", default=0)
    glm_output_tokens = fields.Integer(string="GLM Output Tokens", default=0)
    qc_input_tokens = fields.Integer(string="Prompt QC Input Tokens", default=0)
    qc_output_tokens = fields.Integer(string="Prompt QC Output Tokens", default=0)
    goal_input_tokens = fields.Integer(string="Goal Gen Input Tokens", default=0)
    goal_output_tokens = fields.Integer(string="Goal Gen Output Tokens", default=0)
    rubric_input_tokens = fields.Integer(string="Rubric Gen Input Tokens", default=0)
    rubric_output_tokens = fields.Integer(string="Rubric Gen Output Tokens", default=0)
    rubric_qc_input_tokens = fields.Integer(string="Rubric QC Input Tokens", default=0)
    rubric_qc_output_tokens = fields.Integer(string="Rubric QC Output Tokens", default=0)

    turn_ids = fields.One2many(
        "atlas.turn", "atlas_id", string="Turn History"
    )
    has_turns = fields.Boolean(compute="_compute_has_turns")

    @api.depends("turn_ids")
    def _compute_has_turns(self):
        for rec in self:
            rec.has_turns = bool(rec.turn_ids)

    @api.depends("sandbox_ids", "sandbox_ids.model_type")
    def _compute_sandbox_ids(self):
        for rec in self:
            sandbox = rec.sandbox_ids.filtered(
                lambda s: s.model_type == "glm"
            )[:1]
            rec.glm_sandbox_id = sandbox.id if sandbox else False

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec.ensure_sandboxes()
        return records

    def ensure_sandboxes(self):
        for rec in self:
            existing = rec.sandbox_ids.mapped("model_type")
            if "glm" not in existing:
                self.env["atlas.sandbox"].create(
                    {"atlas_id": rec.id, "model_type": "glm"}
                )

    def _get_all_turns(self, current_session_only=True):
        self.ensure_one()
        turns = self.env["atlas.turn"]
        for sandbox in self.sandbox_ids:
            sb_turns = sandbox.turn_ids
            if current_session_only and sandbox.current_session_id:
                sb_turns = sb_turns.filtered(
                    lambda t, sid=sandbox.current_session_id: t.session_id == sid
                )
            turns |= sb_turns
        return turns.sorted("turn_number")

    def action_view_turns(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Turns",
            "res_model": "atlas.turn",
            "view_mode": "list,form",
            "domain": [("sandbox_id", "in", self.sandbox_ids.ids)],
            "context": {"default_atlas_id": self.id},
        }

    def action_clear_turns(self):
        self.ensure_one()
        turns = self._get_all_turns()
        count = len(turns)
        turns.unlink()
        _logger.info("Cleared %d turns for task %s", count, self.id)

    def action_regenerate_description(self):
        self.ensure_one()
        turns = self._get_all_turns()
        if not turns:
            raise UserError("No user prompts found. Start a sandbox session first.")

        dotenv = _load_dotenv()
        api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        ICP = self.env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()

        if not api_key:
            raise UserError(
                "AWS_BEARER_TOKEN_BEDROCK not found in .env file. "
                "Please set it and restart Odoo."
            )
        if not inference_arn:
            raise UserError(
                "Bedrock Inference ARN not configured. "
                "Go to Settings > Technical > System Parameters and set "
                "'atlas.bedrock_inference_arn' to your Bedrock ARN."
            )

        self.write({
            "goal_generation_status": "running",
            "rubric_generation_status": "running",
        })

        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        from .atlas_sandbox import _GENERATION_POOL, _run_generation_background

        @self.env.cr.postcommit.add
        def _queue_regeneration():
            _GENERATION_POOL.submit(
                _run_generation_background,
                db_name,
                task_id,
                notify_partner_id,
            )

    def action_regenerate_goal(self):
        self.ensure_one()
        turns = self._get_all_turns()
        if not turns:
            raise UserError("No user prompts found. Start a sandbox session first.")

        dotenv = _load_dotenv()
        api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        ICP = self.env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()

        if not api_key:
            raise UserError("AWS_BEARER_TOKEN_BEDROCK not found in .env file.")
        if not inference_arn:
            raise UserError("Bedrock Inference ARN not configured.")

        self.write({"goal_generation_status": "running"})

        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        from .atlas_sandbox import _GENERATION_POOL, _run_goal_only_background

        @self.env.cr.postcommit.add
        def _queue():
            _GENERATION_POOL.submit(
                _run_goal_only_background,
                db_name,
                task_id,
                notify_partner_id,
            )

    def action_regenerate_rubric(self):
        self.ensure_one()
        turns = self._get_all_turns()
        if not turns:
            raise UserError("No user prompts found. Start a sandbox session first.")

        dotenv = _load_dotenv()
        api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        ICP = self.env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()

        if not api_key:
            raise UserError("AWS_BEARER_TOKEN_BEDROCK not found in .env file.")
        if not inference_arn:
            raise UserError("Bedrock Inference ARN not configured.")

        self.write({"rubric_generation_status": "running"})

        task_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        from .atlas_sandbox import _GENERATION_POOL, _run_rubric_only_background

        @self.env.cr.postcommit.add
        def _queue():
            _GENERATION_POOL.submit(
                _run_rubric_only_background,
                db_name,
                task_id,
                notify_partner_id,
            )

    def _deployment_mode(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("atlas.deployment_mode", "local")
            .strip()
        )

    @api.model
    def _cron_reconcile_sandboxes(self):
        self.env["atlas.sandbox"]._cron_reconcile()


class AtlasTurn(models.Model):
    _name = "atlas.turn"
    _description = "Atlas Turn"
    _order = "id asc"

    sandbox_id = fields.Many2one(
        "atlas.sandbox", string="Sandbox", ondelete="cascade", index=True
    )
    atlas_id = fields.Many2one(related="sandbox_id.atlas_id", store=True, readonly=True)
    employee_id = fields.Many2one(
        related="atlas_id.employee_id", store=True, readonly=True
    )
    session_id = fields.Char(string="Session ID", index=True)
    session_label = fields.Char(
        string="Session", compute="_compute_session_label", store=True
    )
    turn_number = fields.Integer(string="Turn Number")
    turn_status = fields.Selection([("Pending", "Pending"), ("Completed", "Completed")])
    prompt = fields.Text(string="Prompt")
    response = fields.Text(string="Response")
    run_id = fields.Char(string="Run ID", index=True)
    model_name = fields.Char(string="Model")
    prompt_timestamp = fields.Char(string="Prompt Timestamp (ISO)")
    response_timestamp = fields.Char(string="Response Timestamp (ISO)")
    tool_calls = fields.Text(string="Tool Calls (JSON)")
    raw_events = fields.Text(string="Raw WS Events (JSON)")
    qc_severity = fields.Selection(
        [
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("critical", "Critical"),
        ],
        string="QC Severity",
    )
    qc_response = fields.Text(string="QC Response (JSON)")
    qc_dismiss_reason = fields.Text(string="QC Dismiss Reason")
    qc_input_tokens = fields.Integer(string="Prompt QC Input Tokens", default=0)
    qc_output_tokens = fields.Integer(string="Prompt QC Output Tokens", default=0)
    glm_input_tokens = fields.Integer(string="GLM Input Tokens", default=0)
    glm_output_tokens = fields.Integer(string="GLM Output Tokens", default=0)
    tool_names = fields.Char(
        string="Tools Used", compute="_compute_tool_names", store=True
    )
    feedback = fields.Selection(
        [("satisfied", "Satisfied"), ("unsatisfied", "Unsatisfied")],
        string="Feedback",
    )
    hints = fields.Text(string="Hints")
    hint_text = fields.Text(string="Hint Text")
    is_hint_turn = fields.Boolean(string="Is Hint Turn", default=False)

    @api.depends("session_id", "atlas_id")
    def _compute_session_label(self):
        task_sessions = {}
        for rec in self:
            task_id = rec.atlas_id.id if rec.atlas_id else 0
            sid = rec.session_id or ""
            if task_id not in task_sessions:
                all_sids = (
                    self.env["atlas.turn"]
                    .search(
                        [("atlas_id", "=", task_id)],
                        order="id asc",
                    )
                    .mapped("session_id")
                )
                seen = []
                for s in all_sids:
                    if s and s not in seen:
                        seen.append(s)
                task_sessions[task_id] = seen

            sessions = task_sessions.get(task_id, [])
            if sid and sid in sessions:
                rec.session_label = "Session %d" % (sessions.index(sid) + 1)
            else:
                rec.session_label = "Session"

    @api.depends("tool_calls")
    def _compute_tool_names(self):
        for rec in self:
            names = []
            if rec.tool_calls:
                try:
                    calls = json.loads(rec.tool_calls)
                    if isinstance(calls, list):
                        for c in calls:
                            n = c.get("name", "")
                            if n and n not in names:
                                names.append(n)
                except (json.JSONDecodeError, TypeError):
                    pass
            rec.tool_names = ", ".join(names) if names else False
