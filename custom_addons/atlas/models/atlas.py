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
_DOTENV_CACHE = {}


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
    inference_arn = dotenv.get("KIMI_BEDROCK_MODEL_ARN", "").strip()
    region = dotenv.get("KIMI_AWS_REGION", "us-east-1").strip()

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

    # Strip ALL fenced code blocks (formula blocks, prose blocks) — the rubric
    # table itself is emitted in raw markdown, never inside a fence. Keeping
    # fenced content would cause the non-greedy regex to grab a non-table
    # fence (e.g. the Formula block) and discard the real table above it.
    parse_text = _re.sub(r"```[\s\S]*?```", "", text)

    _QC_JUNK_PATTERNS = (
        "\u2713", "\u2717", "\u2714", "\u2716", "check ", "verdict",
        "self-qc", "qc note", "verification", "weakest",
    )

    _SECTION_STOP_PATTERNS = (
        "### scoring", "## scoring", "### self-qc", "### verification",
        "### formula", "score range", "maxraw", "score =",
    )

    def _is_separator(line):
        stripped = line.strip().strip("|").strip()
        return bool(_re.match(r"^[\s\-:| ]+$", stripped))

    def _is_header(line):
        cols = [c.strip().lower() for c in line.split("|") if c.strip()]
        header_words = {"criterion", "criteria", "category", "importance", "#", "levels", "suggestion", "+/-"}
        matches = sum(1 for c in cols if c in header_words)
        return matches >= 3

    def _is_qc_junk(line):
        inner = line.strip().strip("|").strip().lower()
        if any(inner.startswith(p) for p in _QC_JUNK_PATTERNS):
            return True
        if _re.match(r"^#\d+\s", inner):
            return True
        if any(kw in inner for kw in (
            "self-contained", "objective?", "fact-stable", "total negative",
            "score range", "interpretation", "poor:", "needs improvement",
            "excellent:", "good:", "negative?", "negatives?",
        )):
            return True
        return False

    table_lines = []
    hit_stop_section = False
    for line in parse_text.split("\n"):
        stripped = line.strip()
        lower_stripped = stripped.lower()
        if any(lower_stripped.startswith(p) for p in _SECTION_STOP_PATTERNS):
            hit_stop_section = True
        if hit_stop_section:
            continue
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
                dc_raw = dc.strip()
                if dc_raw and dc_lower not in VALID_IMPS and len(dc_raw) < 50 and not _re.match(r"^[\d✅❌+\-]+$", dc_raw):
                    custom_category = dc_raw

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
            level_matches = _re.findall(r"(-?\d+)\s*:\s*([^|]+?)(?=\s*-?\d+\s*:|$)", col)
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
            "weight": max((abs(lv["score"]) for lv in levels), default=2),
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
    inference_arn = dotenv.get("KIMI_BEDROCK_MODEL_ARN", "").strip()
    region = dotenv.get("KIMI_AWS_REGION", "us-east-1").strip()

    if not api_key or not inference_arn:
        return [], {}

    # Rubric is derived solely from user queries. We intentionally ignore
    # task.goal_description and the assistant (GLM-5) responses so the
    # criteria reflect what the user asked for, not the model's answer or
    # any pre-generated goal metadata.
    sent_turns = [t for t in turns if t.prompt and not t.is_hint_turn]
    if not sent_turns:
        _logger.warning(
            "generate_rubric_from_turns: no user queries found (total turns=%d)",
            len(turns) if hasattr(turns, "__len__") else -1,
        )
        return [], {}

    user_queries = [t.prompt.strip()[:800] for t in sent_turns if t.prompt and t.prompt.strip()]
    if not user_queries:
        _logger.warning("generate_rubric_from_turns: all user queries were empty after strip")
        return [], {}

    user_message = "## User Queries\n%s" % "\n".join(
        "%d. %s" % (idx, q) for idx, q in enumerate(user_queries, start=1)
    )

    try:
        from ..controllers.llm_assisst_qc import _call_bedrock_converse

        response_text, usage = _call_bedrock_converse(
            api_key=api_key,
            inference_arn=inference_arn,
            region=region,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=8192,
            temperature=0.3,
            timeout=120.0,
        )
        _logger.info("generate_rubric_from_turns: response_len=%d tokens=%s", len(response_text), usage)
        if env["ir.config_parameter"].sudo().get_param("atlas.log_llm_responses", "False") == "True":
            _logger.info("generate_rubric_from_turns: ===== FULL RESPONSE BEGIN (task=%s) =====", task_id)
            for _chunk_start in range(0, len(response_text), 2000):
                _logger.info("generate_rubric_from_turns: FULL[%d:%d]: %s",
                             _chunk_start, _chunk_start + 2000,
                             response_text[_chunk_start:_chunk_start + 2000])
            _logger.info("generate_rubric_from_turns: ===== FULL RESPONSE END (task=%s) =====", task_id)
        else:
            _logger.debug("generate_rubric_from_turns: FULL RESPONSE (task=%s): %s", task_id, response_text)

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

    dotenv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.isfile(dotenv_path):
        try:
            mtime = os.path.getmtime(dotenv_path)
        except OSError:
            mtime = None
        cached = _DOTENV_CACHE.get(dotenv_path)
        if cached and cached[0] == mtime:
            parsed = cached[1]
        else:
            parsed = {}
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
                    if key.startswith("ATLAS_"):
                        key = key[len("ATLAS_"):]
                    parsed[key] = value
            if mtime is not None:
                _DOTENV_CACHE[dotenv_path] = (mtime, parsed)
            _logger.debug("Loaded .env from %s", dotenv_path)
        env.update(parsed)

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

litellm_settings:
  drop_params: true
  modify_params: true
  telemetry: false
  num_retries: 1
  request_timeout: 900
  stream_timeout: 60

general_settings:
  master_key: os.environ/ATLAS_LITELLM_MASTER_KEY
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
                session_turns = sb_turns.filtered(
                    lambda t, sid=sandbox.current_session_id: t.session_id == sid
                )
                if session_turns:
                    turns |= session_turns
                else:
                    all_sessions = sb_turns.mapped("session_id")
                    seen = []
                    for sid in all_sessions:
                        if sid and sid not in seen:
                            seen.append(sid)
                    if seen:
                        latest_sid = seen[-1]
                        turns |= sb_turns.filtered(
                            lambda t, sid=latest_sid: t.session_id == sid
                        )
                    else:
                        turns |= sb_turns
            else:
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
        inference_arn = dotenv.get("KIMI_BEDROCK_MODEL_ARN", "").strip()

        if not api_key:
            raise UserError(
                "ATLAS_AWS_BEARER_TOKEN_BEDROCK not found in .env file. "
                "Please set it and restart Odoo."
            )
        if not inference_arn:
            raise UserError(
                "ATLAS_KIMI_BEDROCK_MODEL_ARN not configured in .env file."
            )

        self.env.cr.execute(
            "UPDATE atlas_atlas "
            "SET goal_generation_status = 'running', rubric_generation_status = 'running' "
            "WHERE id = %s "
            "AND goal_generation_status != 'running' "
            "AND rubric_generation_status != 'running'",
            (self.id,),
        )
        if self.env.cr.rowcount == 0:
            raise UserError("Regeneration is already in progress for this task.")
        self.invalidate_recordset(["goal_generation_status", "rubric_generation_status"])

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
        inference_arn = dotenv.get("KIMI_BEDROCK_MODEL_ARN", "").strip()

        if not api_key:
            raise UserError("ATLAS_AWS_BEARER_TOKEN_BEDROCK not found in .env file.")
        if not inference_arn:
            raise UserError("ATLAS_KIMI_BEDROCK_MODEL_ARN not configured in .env file.")

        self.env.cr.execute(
            "UPDATE atlas_atlas SET goal_generation_status = 'running' "
            "WHERE id = %s AND goal_generation_status != 'running'",
            (self.id,),
        )
        if self.env.cr.rowcount == 0:
            raise UserError("Goal regeneration is already in progress for this task.")
        self.invalidate_recordset(["goal_generation_status"])

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
        inference_arn = dotenv.get("KIMI_BEDROCK_MODEL_ARN", "").strip()

        if not api_key:
            raise UserError("ATLAS_AWS_BEARER_TOKEN_BEDROCK not found in .env file.")
        if not inference_arn:
            raise UserError("ATLAS_KIMI_BEDROCK_MODEL_ARN not configured in .env file.")

        self.env.cr.execute(
            "UPDATE atlas_atlas SET rubric_generation_status = 'running' "
            "WHERE id = %s AND rubric_generation_status != 'running'",
            (self.id,),
        )
        if self.env.cr.rowcount == 0:
            raise UserError("Rubric regeneration is already in progress for this task.")
        self.invalidate_recordset(["rubric_generation_status"])

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

    @api.model
    def _cron_recover_stuck_generation(self):
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=15)
        stuck = self.sudo().search([
            "|",
            ("goal_generation_status", "=", "running"),
            ("rubric_generation_status", "=", "running"),
            ("write_date", "<", cutoff),
        ])
        if not stuck:
            return
        for task in stuck:
            vals = {}
            if task.goal_generation_status == "running":
                vals["goal_generation_status"] = "error"
            if task.rubric_generation_status == "running":
                vals["rubric_generation_status"] = "error"
            if vals:
                task.write(vals)
                _logger.warning(
                    "Stuck-generation recovery: task=%s reset %s (write_date=%s)",
                    task.id, list(vals.keys()), task.write_date,
                )


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
    qc_justification = fields.Text(
        string="QC Justification",
        help="Reviewer-provided justification for keeping the original prompt "
             "despite a medium-severity QC verdict. Not applicable for low "
             "(no action needed) or high/critical (rewrite mandatory).",
    )
    qc_status_display = fields.Html(
        string="QC Status",
        compute="_compute_qc_status_display",
        sanitize=False,
    )
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

    @api.depends("qc_severity", "qc_justification")
    def _compute_qc_status_display(self):
        colors = {
            "low": "#198754",
            "medium": "#ffc107",
            "high": "#fd7e14",
            "critical": "#dc3545",
        }
        for rec in self:
            sev = rec.qc_severity or ""
            if not sev:
                rec.qc_status_display = False
                continue
            sev_label = sev.capitalize()
            color = colors.get(sev, "#6c757d")
            badge = (
                '<span style="display:inline-block;padding:2px 8px;'
                'border-radius:10px;background:%s;color:#fff;'
                'font-weight:600;font-size:0.85em;">%s</span>'
            ) % (color, sev_label)
            justif = (rec.qc_justification or "").strip()
            if justif:
                safe = (
                    justif.replace("&", "&amp;")
                          .replace("<", "&lt;")
                          .replace(">", "&gt;")
                )
                rec.qc_status_display = (
                    '%s<div style="margin-top:4px;color:#664d03;'
                    'background:#fff3cd;border:1px solid #ffe69c;'
                    'border-radius:4px;padding:4px 6px;font-size:0.85em;'
                    'white-space:pre-wrap;">%s</div>'
                ) % (badge, safe)
            else:
                rec.qc_status_display = badge

    @api.depends("tool_calls")
    def _compute_tool_names(self):
        for rec in self:
            names = []
            if rec.tool_calls:
                try:
                    calls = json.loads(rec.tool_calls)
                    if isinstance(calls, list):
                        for c in calls:
                            if not isinstance(c, dict):
                                continue
                            n = c.get("name", "")
                            if n and n not in names:
                                names.append(n)
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass
            rec.tool_names = ", ".join(names) if names else False

    @api.constrains("sandbox_id", "session_id", "turn_number")
    def _check_unique_turn_number(self):
        for rec in self:
            if not rec.sandbox_id or not rec.turn_number:
                continue
            domain = [
                ("sandbox_id", "=", rec.sandbox_id.id),
                ("session_id", "=", rec.session_id or ""),
                ("turn_number", "=", rec.turn_number),
                ("id", "!=", rec.id),
            ]
            dup = self.search_count(domain)
            if dup:
                from odoo.exceptions import ValidationError
                raise ValidationError(
                    "Duplicate turn number %s in session %s (sandbox %s)."
                    % (rec.turn_number, rec.session_id or "-", rec.sandbox_id.id)
                )
