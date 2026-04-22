# -*- coding: utf-8 -*-
import logging
import os
import threading
import time as _time_mod
from concurrent.futures import ThreadPoolExecutor

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError
from odoo.modules.registry import Registry

from ..controllers.llm_actions import (
    eval_all_responses_kimi,
    kimi_assist_eval,
    perform_qc_checks_kimi,
    _extract_eval_scores,
    _to_selection_score,
    generate_responses_from_prompt,
    generate_criteria_from_prompt as generate_rubrics_from_prompt,
)
from ..controllers import llm_actions as _llm_mod

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Bounded background thread pool for eval jobs
# ──────────────────────────────────────────────────────────────
_EVAL_POOL_WORKERS = int(os.getenv("EVAL_POOL_WORKERS", "80"))
_EVAL_POOL = ThreadPoolExecutor(
    max_workers=_EVAL_POOL_WORKERS, thread_name_prefix="berserker_eval_bg"
)

# Dedup guard — prevents duplicate threads evaluating the same record
_EVAL_INFLIGHT = set()
_EVAL_LOCK = threading.Lock()

SELECTION_1_6 = [
    ("1", "1"),
    ("2", "2"),
    ("3", "3"),
    ("4", "4"),
    ("5", "5"),
    ("6", "6"),
]


def check_error(value, value2):
    minus_val = value - 1
    plus_val = value + 1
    check = True if value2 > plus_val or value2 < minus_val else False
    return check


# ──────────────────────────────────────────────────────────────
# Background eval worker (module-level, cursor-efficient)
# ──────────────────────────────────────────────────────────────
def _run_eval_background(db_name, record_id, notify_partner_id=None):
    _llm_mod._config_cache_db = db_name

    with _EVAL_LOCK:
        if record_id in _EVAL_INFLIGHT:
            _logger.warning(
                "Skipping duplicate eval for record %s — already in-flight", record_id
            )
            return
        _EVAL_INFLIGHT.add(record_id)

    try:
        # Phase 1: Brief cursor for reading snapshot
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            rec = env["berserker"].browse(record_id)
            if not rec.exists():
                _logger.warning("Record %s not found, skipping eval", record_id)
                return
            # DB-level dedup: skip if already being evaluated by another worker
            if rec.eval_status == "done":
                _logger.info("Record %s already done, skipping eval", record_id)
                return
            snapshot = {
                "task_id": rec.task_id or "",
                "client_prompt": rec.client_prompt or "",
                "gpt_response": rec.gpt_response or "",
                "gemini_response": rec.gemini_response or "",
                "claude_response": rec.claude_response or "",
            }
            rubric_names = []
            for n in range(1, 6):
                rname = getattr(rec, f"rubric{n}_name", "") or ""
                if rname.strip():
                    rubric_names.append(rname.strip())

        # Phase 2: LLM calls — NO open cursor
        api_key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        _llm_mod.set_usage_context(berserker_id=record_id, call_type="kimi_assist")
        eval_results = kimi_assist_eval(
            api_key=api_key,
            prompt=snapshot["client_prompt"],
            gpt_response=snapshot["gpt_response"],
            gemini_response=snapshot["gemini_response"],
            claude_response=snapshot["claude_response"],
            rubric_names=rubric_names,
        )

        # Phase 3: Build accumulated vals from eval results
        accumulated_vals = {"is_processed": True}
        MODELS = ["gpt", "gemini", "claude"]
        for model_key in MODELS:
            model_eval = eval_results.get(model_key, {})
            if "error" in model_eval:
                _logger.warning(
                    "Eval error for %s on record %s: %s",
                    model_key,
                    record_id,
                    model_eval["error"],
                )
                continue
            scores = _extract_eval_scores(model_eval)
            for dim, data in scores.items():
                score_str = _to_selection_score(data["score"], 1, 6)
                if score_str:
                    accumulated_vals[f"store_{model_key}_{dim}"] = score_str
                    accumulated_vals[f"{model_key}_{dim}"] = score_str
                if data["reason"]:
                    accumulated_vals[f"reason1_{model_key}_{dim}"] = data["reason"]

        justification = eval_results.get("justification", "")
        if justification:
            accumulated_vals["justification"] = justification
            accumulated_vals["store_justification"] = justification

        _eval_write_results(db_name, record_id, accumulated_vals, notify_partner_id)

        if rubric_names:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                rec = env["berserker"].browse(record_id)
                rubric_write_vals = {}
                for model_key in MODELS:
                    model_rubrics = eval_results.get(model_key, {}).get("rubrics", {})
                    for idx, rname in enumerate(rubric_names):
                        n = idx + 1
                        raw_score = model_rubrics.get(rname)
                        if raw_score is not None:
                            score_str = _to_selection_score(raw_score, 1, 6)
                            if score_str:
                                rubric_write_vals[
                                    f"store_{model_key}_rubric{n}_rating"
                                ] = score_str
                                rubric_write_vals[f"{model_key}_rubric{n}_rating"] = (
                                    score_str
                                )
                if rubric_write_vals:
                    rec._safe_write(rubric_write_vals, label="rubric_ratings")

        from ..controllers.llm_actions import flush_usage_logs

        flush_usage_logs()

    except Exception as e:
        _logger.error(
            "Eval pipeline failed for record %s: %s", record_id, e, exc_info=True
        )
        _eval_mark_failed(db_name, record_id, notify_partner_id)
    finally:
        with _EVAL_LOCK:
            _EVAL_INFLIGHT.discard(record_id)


def _run_full_pipeline_background(db_name, record_id, notify_partner_id=None):
    _llm_mod._config_cache_db = db_name

    with _EVAL_LOCK:
        if record_id in _EVAL_INFLIGHT:
            _logger.warning("Skipping duplicate full pipeline for record %s", record_id)
            return
        _EVAL_INFLIGHT.add(record_id)

    try:
        # Phase 1: Read prompt snapshot
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            rec = env["berserker"].browse(record_id)
            if not rec.exists():
                _logger.warning("Record %s not found", record_id)
                return
            # DB-level dedup: skip if already done
            if rec.eval_status == "done":
                _logger.info(
                    "Record %s already done, skipping full pipeline", record_id
                )
                return
            prompt = rec.client_prompt or ""

        if not prompt.strip():
            _logger.warning("Record %s has empty prompt, skipping", record_id)
            _eval_mark_failed(db_name, record_id, notify_partner_id)
            return

        # Phase 2: Generate responses (NO cursor) — calls GPT 5.4, Gemini 3.1, Claude 4.7 in parallel
        _logger.info("Phase 2: Generating responses for record %s", record_id)
        _llm_mod.set_usage_context(berserker_id=record_id, call_type="response_gen")
        responses = generate_responses_from_prompt(prompt)

        # Phase 3: Write responses to record
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            rec = env["berserker"].browse(record_id)
            rec._safe_write(
                {
                    "gpt_response": responses.get("gpt_response", ""),
                    "gemini_response": responses.get("gemini_response", ""),
                    "claude_response": responses.get("claude_response", ""),
                },
                label="responses",
            )

        # Phase 4: Generate criteria (NO cursor)
        _logger.info("Phase 4: Generating criteria for record %s", record_id)
        _llm_mod.set_usage_context(berserker_id=record_id, call_type="rubric_gen")
        rubric_list = generate_rubrics_from_prompt(prompt)

        # Phase 5: Write rubric names to flat fields
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            rec = env["berserker"].browse(record_id)
            rubric_name_vals = {}
            for idx, rname in enumerate(rubric_list[:5]):
                n = idx + 1
                rubric_name_vals[f"rubric{n}_name"] = rname
                rubric_name_vals[f"store_rubric{n}_name"] = rname
            rec._safe_write(rubric_name_vals, label="rubric_names")

        # Phase 6: Evaluate responses via Kimi Assist (NO cursor)
        # Read rubric names from flat fields
        rubric_names = []
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            rec = env["berserker"].browse(record_id)
            for n in range(1, 6):
                rname = getattr(rec, f"rubric{n}_name", "") or ""
                if rname.strip():
                    rubric_names.append(rname.strip())

        _logger.info("Phase 6: Evaluating responses for record %s", record_id)
        api_key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        _llm_mod.set_usage_context(berserker_id=record_id, call_type="kimi_assist")
        eval_results = kimi_assist_eval(
            api_key=api_key,
            prompt=prompt,
            gpt_response=responses.get("gpt_response", ""),
            gemini_response=responses.get("gemini_response", ""),
            claude_response=responses.get("claude_response", ""),
            rubric_names=rubric_names,
        )

        # Phase 7: Write eval results + justification
        accumulated_vals = {"is_processed": True}
        MODELS = ["gpt", "gemini", "claude"]
        for model_key in MODELS:
            model_eval = eval_results.get(model_key, {})
            if "error" in model_eval:
                _logger.warning(
                    "Eval error for %s on record %s: %s",
                    model_key,
                    record_id,
                    model_eval["error"],
                )
                continue
            scores = _extract_eval_scores(model_eval)
            for dim, data in scores.items():
                score_str = _to_selection_score(data["score"], 1, 6)
                if score_str:
                    accumulated_vals[f"store_{model_key}_{dim}"] = score_str
                    accumulated_vals[f"{model_key}_{dim}"] = score_str
                if data["reason"]:
                    accumulated_vals[f"reason1_{model_key}_{dim}"] = data["reason"]

        justification = eval_results.get("justification", "")
        if justification:
            accumulated_vals["justification"] = justification
            accumulated_vals["store_justification"] = justification

        _eval_write_results(db_name, record_id, accumulated_vals, notify_partner_id)

        # Phase 7b: Write rubric scale ratings to flat fields
        if rubric_names:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                rec = env["berserker"].browse(record_id)
                rubric_write_vals = {}
                for model_key in MODELS:
                    model_rubrics = eval_results.get(model_key, {}).get("rubrics", {})
                    for idx, rname in enumerate(rubric_names):
                        n = idx + 1
                        raw_score = model_rubrics.get(rname)
                        if raw_score is not None:
                            score_str = _to_selection_score(raw_score, 1, 6)
                            if score_str:
                                rubric_write_vals[
                                    f"store_{model_key}_rubric{n}_rating"
                                ] = score_str
                                rubric_write_vals[f"{model_key}_rubric{n}_rating"] = (
                                    score_str
                                )
                if rubric_write_vals:
                    rec._safe_write(rubric_write_vals, label="rubric_ratings")

        _logger.info("Full pipeline completed for record %s", record_id)

        from ..controllers.llm_actions import flush_usage_logs

        flush_usage_logs()

    except Exception as e:
        _logger.error(
            "Full pipeline failed for record %s: %s", record_id, e, exc_info=True
        )
        _eval_mark_failed(db_name, record_id, notify_partner_id)
    finally:
        with _EVAL_LOCK:
            _EVAL_INFLIGHT.discard(record_id)


def _eval_write_results(db_name, record_id, accumulated_vals, notify_partner_id=None):
    with Registry(db_name).cursor() as new_cr:
        new_env = api.Environment(new_cr, SUPERUSER_ID, {})
        rec = new_env["berserker"].browse(record_id)

        if accumulated_vals:
            accumulated_vals["eval_status"] = "done"
            rec._safe_write(accumulated_vals, label="eval_all")
        else:
            rec._safe_write({"eval_status": "done"}, label="eval_status")

        # Bus notification
        try:
            partner = None
            if notify_partner_id:
                partner = new_env["res.partner"].browse(notify_partner_id)
                if not partner.exists():
                    partner = None
            if not partner:
                partner = rec.user_id.partner_id or rec.create_uid.partner_id
            if partner:
                new_env["bus.bus"]._sendone(
                    partner,
                    "berserker/eval_done",
                    {
                        "record_id": record_id,
                        "task_id": rec.task_id or "",
                        "eval_status": "done",
                    },
                )
        except Exception:
            _logger.warning(
                "Bus notification failed for record %s (non-critical)",
                record_id,
                exc_info=True,
            )


def _eval_mark_failed(db_name, record_id, notify_partner_id=None):
    try:
        with Registry(db_name).cursor() as new_cr:
            new_env = api.Environment(new_cr, SUPERUSER_ID, {})
            rec = new_env["berserker"].browse(record_id)
            if rec.eval_status == "done":
                _logger.info(
                    "Skipping eval_status=failed for record %s — already done",
                    record_id,
                )
                return
            rec._safe_write({"eval_status": "failed"}, label="eval_failed")
            # Bus notification
            try:
                partner = None
                if notify_partner_id:
                    partner = new_env["res.partner"].browse(notify_partner_id)
                    if not partner.exists():
                        partner = None
                if not partner:
                    partner = rec.user_id.partner_id or rec.create_uid.partner_id
                if partner:
                    new_env["bus.bus"]._sendone(
                        partner,
                        "berserker/eval_done",
                        {
                            "record_id": record_id,
                            "task_id": rec.task_id or "",
                            "eval_status": "failed",
                        },
                    )
            except Exception:
                _logger.warning(
                    "Bus notification failed for record %s", record_id, exc_info=True
                )
    except Exception:
        _logger.error(
            "Failed to mark eval_status=failed for record %s", record_id, exc_info=True
        )


class Berserker(models.Model):
    _name = "berserker"
    _description = "Berserker"
    _rec_name = "task_id"

    active = fields.Boolean(default=True, string="Active")

    @api.model
    def fields_get(self, allfields=None, attributes=None):
        res = super().fields_get(allfields, attributes)
        if not self.env.user.has_group("berserker.group_berserker_admin"):
            res.pop("active", None)
        return res

    def action_archive(self):
        if not self.env.user.has_group("berserker.group_berserker_admin"):
            raise AccessError(_("Only Berserker Admins can archive records."))
        return super().action_archive()

    def action_unarchive(self):
        if not self.env.user.has_group("berserker.group_berserker_admin"):
            raise AccessError(_("Only Berserker Admins can unarchive records."))
        return super().action_unarchive()

    def _compute_is_tasker(self):
        has_group = self.env.user.has_group("berserker.group_berserker_user")
        for record in self:
            record.is_tasker = has_group

    # ──────────────────────────────────────────────────────────────
    # Metadata / status fields
    # ──────────────────────────────────────────────────────────────
    task_id = fields.Char()
    task_status = fields.Selection(
        [
            ("NotSubmitted", "Not Submitted"),
            ("Submitted", "Submitted"),
        ]
    )
    employee_id = fields.Many2one("hr.employee")
    user_id = fields.Many2one(related="employee_id.user_id")
    is_processed = fields.Boolean()
    is_eval_done = fields.Boolean()
    eval_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("evaluating", "Evaluating"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="idle",
        string="Eval Status",
    )
    is_tasker = fields.Boolean(compute="_compute_is_tasker")
    submitted_at = fields.Datetime(string="Submitted At")
    qc_task_status = fields.Selection([("pass", "Pass"), ("fail", "Fail")])
    qc_score = fields.Integer(string="QC score")

    # ──────────────────────────────────────────────────────────────
    # Prompt & Responses
    # ──────────────────────────────────────────────────────────────
    client_prompt = fields.Text()
    gpt_response = fields.Text()
    gemini_response = fields.Text()
    claude_response = fields.Text()

    # ──────────────────────────────────────────────────────────────
    # Ranking (drag-and-drop order: "gpt,gemini,claude" = GPT 1st, Gemini 2nd, Claude 3rd)
    # ──────────────────────────────────────────────────────────────
    ranking_order = fields.Char(
        string="Ranking Order",
        default="gpt,gemini,claude",
        help="Comma-separated model order from best (1st) to worst (3rd).",
    )
    gpt_rank = fields.Selection(
        [("1", "1st"), ("2", "2nd"), ("3", "3rd")],
        string="GPT Rank",
        compute="_compute_ranks",
        store=True,
    )
    gemini_rank = fields.Selection(
        [("1", "1st"), ("2", "2nd"), ("3", "3rd")],
        string="Gemini Rank",
        compute="_compute_ranks",
        store=True,
    )
    claude_rank = fields.Selection(
        [("1", "1st"), ("2", "2nd"), ("3", "3rd")],
        string="Claude Rank",
        compute="_compute_ranks",
        store=True,
    )

    @api.depends("ranking_order")
    def _compute_ranks(self):
        for rec in self:
            order = (rec.ranking_order or "gpt,gemini,claude").split(",")
            order = [m.strip() for m in order if m.strip()]
            rank_map = {}
            for idx, model in enumerate(order):
                rank_map[model] = str(idx + 1)
            rec.gpt_rank = rank_map.get("gpt", "1")
            rec.gemini_rank = rank_map.get("gemini", "2")
            rec.claude_rank = rank_map.get("claude", "3")

    # ──────────────────────────────────────────────────────────────
    # Justification
    # ──────────────────────────────────────────────────────────────
    justification = fields.Text()
    store_justification = fields.Text()
    error_justification = fields.Boolean(default=False)

    # ──────────────────────────────────────────────────────────────
    # Rubrics (up to 5, flat fields)
    # ──────────────────────────────────────────────────────────────
    rubric1_name = fields.Text(string="Rubric 1")
    rubric2_name = fields.Text(string="Rubric 2")
    rubric3_name = fields.Text(string="Rubric 3")
    rubric4_name = fields.Text(string="Rubric 4")
    rubric5_name = fields.Text(string="Rubric 5")

    has_rubric1 = fields.Boolean(compute="_compute_has_rubrics")
    has_rubric2 = fields.Boolean(compute="_compute_has_rubrics")
    has_rubric3 = fields.Boolean(compute="_compute_has_rubrics")
    has_rubric4 = fields.Boolean(compute="_compute_has_rubrics")
    has_rubric5 = fields.Boolean(compute="_compute_has_rubrics")

    @api.depends(
        "rubric1_name", "rubric2_name", "rubric3_name", "rubric4_name", "rubric5_name"
    )
    def _compute_has_rubrics(self):
        for rec in self:
            rec.has_rubric1 = bool(rec.rubric1_name and rec.rubric1_name.strip())
            rec.has_rubric2 = bool(rec.rubric2_name and rec.rubric2_name.strip())
            rec.has_rubric3 = bool(rec.rubric3_name and rec.rubric3_name.strip())
            rec.has_rubric4 = bool(rec.rubric4_name and rec.rubric4_name.strip())
            rec.has_rubric5 = bool(rec.rubric5_name and rec.rubric5_name.strip())

    store_rubric1_name = fields.Text()
    store_rubric2_name = fields.Text()
    store_rubric3_name = fields.Text()
    store_rubric4_name = fields.Text()
    store_rubric5_name = fields.Text()

    error_rubric1_name = fields.Boolean(default=False)
    error_rubric2_name = fields.Boolean(default=False)
    error_rubric3_name = fields.Boolean(default=False)
    error_rubric4_name = fields.Boolean(default=False)
    error_rubric5_name = fields.Boolean(default=False)

    reason1_rubric1_name = fields.Text()
    reason1_rubric2_name = fields.Text()
    reason1_rubric3_name = fields.Text()
    reason1_rubric4_name = fields.Text()
    reason1_rubric5_name = fields.Text()

    # ──────────────────────────────────────────────────────────────
    # GPT rubric ratings
    # ──────────────────────────────────────────────────────────────
    gpt_rubric1_rating = fields.Selection(SELECTION_1_6, string="GPT Rubric 1")
    gpt_rubric2_rating = fields.Selection(SELECTION_1_6, string="GPT Rubric 2")
    gpt_rubric3_rating = fields.Selection(SELECTION_1_6, string="GPT Rubric 3")
    gpt_rubric4_rating = fields.Selection(SELECTION_1_6, string="GPT Rubric 4")
    gpt_rubric5_rating = fields.Selection(SELECTION_1_6, string="GPT Rubric 5")

    store_gpt_rubric1_rating = fields.Selection(SELECTION_1_6)
    store_gpt_rubric2_rating = fields.Selection(SELECTION_1_6)
    store_gpt_rubric3_rating = fields.Selection(SELECTION_1_6)
    store_gpt_rubric4_rating = fields.Selection(SELECTION_1_6)
    store_gpt_rubric5_rating = fields.Selection(SELECTION_1_6)

    reason1_gpt_rubric1_rating = fields.Text()
    reason1_gpt_rubric2_rating = fields.Text()
    reason1_gpt_rubric3_rating = fields.Text()
    reason1_gpt_rubric4_rating = fields.Text()
    reason1_gpt_rubric5_rating = fields.Text()

    error_gpt_rubric1_rating = fields.Boolean(default=False)
    error_gpt_rubric2_rating = fields.Boolean(default=False)
    error_gpt_rubric3_rating = fields.Boolean(default=False)
    error_gpt_rubric4_rating = fields.Boolean(default=False)
    error_gpt_rubric5_rating = fields.Boolean(default=False)

    # ──────────────────────────────────────────────────────────────
    # Gemini rubric ratings
    # ──────────────────────────────────────────────────────────────
    gemini_rubric1_rating = fields.Selection(SELECTION_1_6, string="Gemini Rubric 1")
    gemini_rubric2_rating = fields.Selection(SELECTION_1_6, string="Gemini Rubric 2")
    gemini_rubric3_rating = fields.Selection(SELECTION_1_6, string="Gemini Rubric 3")
    gemini_rubric4_rating = fields.Selection(SELECTION_1_6, string="Gemini Rubric 4")
    gemini_rubric5_rating = fields.Selection(SELECTION_1_6, string="Gemini Rubric 5")

    store_gemini_rubric1_rating = fields.Selection(SELECTION_1_6)
    store_gemini_rubric2_rating = fields.Selection(SELECTION_1_6)
    store_gemini_rubric3_rating = fields.Selection(SELECTION_1_6)
    store_gemini_rubric4_rating = fields.Selection(SELECTION_1_6)
    store_gemini_rubric5_rating = fields.Selection(SELECTION_1_6)

    reason1_gemini_rubric1_rating = fields.Text()
    reason1_gemini_rubric2_rating = fields.Text()
    reason1_gemini_rubric3_rating = fields.Text()
    reason1_gemini_rubric4_rating = fields.Text()
    reason1_gemini_rubric5_rating = fields.Text()

    error_gemini_rubric1_rating = fields.Boolean(default=False)
    error_gemini_rubric2_rating = fields.Boolean(default=False)
    error_gemini_rubric3_rating = fields.Boolean(default=False)
    error_gemini_rubric4_rating = fields.Boolean(default=False)
    error_gemini_rubric5_rating = fields.Boolean(default=False)

    # ──────────────────────────────────────────────────────────────
    # Claude rubric ratings
    # ──────────────────────────────────────────────────────────────
    claude_rubric1_rating = fields.Selection(SELECTION_1_6, string="Claude Rubric 1")
    claude_rubric2_rating = fields.Selection(SELECTION_1_6, string="Claude Rubric 2")
    claude_rubric3_rating = fields.Selection(SELECTION_1_6, string="Claude Rubric 3")
    claude_rubric4_rating = fields.Selection(SELECTION_1_6, string="Claude Rubric 4")
    claude_rubric5_rating = fields.Selection(SELECTION_1_6, string="Claude Rubric 5")

    store_claude_rubric1_rating = fields.Selection(SELECTION_1_6)
    store_claude_rubric2_rating = fields.Selection(SELECTION_1_6)
    store_claude_rubric3_rating = fields.Selection(SELECTION_1_6)
    store_claude_rubric4_rating = fields.Selection(SELECTION_1_6)
    store_claude_rubric5_rating = fields.Selection(SELECTION_1_6)

    reason1_claude_rubric1_rating = fields.Text()
    reason1_claude_rubric2_rating = fields.Text()
    reason1_claude_rubric3_rating = fields.Text()
    reason1_claude_rubric4_rating = fields.Text()
    reason1_claude_rubric5_rating = fields.Text()

    error_claude_rubric1_rating = fields.Boolean(default=False)
    error_claude_rubric2_rating = fields.Boolean(default=False)
    error_claude_rubric3_rating = fields.Boolean(default=False)
    error_claude_rubric4_rating = fields.Boolean(default=False)
    error_claude_rubric5_rating = fields.Boolean(default=False)

    # ──────────────────────────────────────────────────────────────
    # GPT rating fields
    # ──────────────────────────────────────────────────────────────
    gpt_truthfulness = fields.Selection(SELECTION_1_6)
    gpt_instruction_following = fields.Selection(SELECTION_1_6)
    gpt_writing_quality = fields.Selection(SELECTION_1_6)
    gpt_verbosity = fields.Selection(SELECTION_1_6)
    gpt_prompt_correctness = fields.Selection(SELECTION_1_6)
    gpt_overall_quality = fields.Selection(SELECTION_1_6)

    # ──────────────────────────────────────────────────────────────
    # Gemini rating fields
    # ──────────────────────────────────────────────────────────────
    gemini_truthfulness = fields.Selection(SELECTION_1_6)
    gemini_instruction_following = fields.Selection(SELECTION_1_6)
    gemini_writing_quality = fields.Selection(SELECTION_1_6)
    gemini_verbosity = fields.Selection(SELECTION_1_6)
    gemini_prompt_correctness = fields.Selection(SELECTION_1_6)
    gemini_overall_quality = fields.Selection(SELECTION_1_6)

    # ──────────────────────────────────────────────────────────────
    # Claude rating fields
    # ──────────────────────────────────────────────────────────────
    claude_truthfulness = fields.Selection(SELECTION_1_6)
    claude_instruction_following = fields.Selection(SELECTION_1_6)
    claude_writing_quality = fields.Selection(SELECTION_1_6)
    claude_verbosity = fields.Selection(SELECTION_1_6)
    claude_prompt_correctness = fields.Selection(SELECTION_1_6)
    claude_overall_quality = fields.Selection(SELECTION_1_6)

    # ──────────────────────────────────────────────────────────────
    # fields to store in db only start
    # ──────────────────────────────────────────────────────────────
    store_gpt_truthfulness = fields.Selection(SELECTION_1_6)
    store_gpt_instruction_following = fields.Selection(SELECTION_1_6)
    store_gpt_writing_quality = fields.Selection(SELECTION_1_6)
    store_gpt_verbosity = fields.Selection(SELECTION_1_6)
    store_gpt_prompt_correctness = fields.Selection(SELECTION_1_6)
    store_gpt_overall_quality = fields.Selection(SELECTION_1_6)

    store_gemini_truthfulness = fields.Selection(SELECTION_1_6)
    store_gemini_instruction_following = fields.Selection(SELECTION_1_6)
    store_gemini_writing_quality = fields.Selection(SELECTION_1_6)
    store_gemini_verbosity = fields.Selection(SELECTION_1_6)
    store_gemini_prompt_correctness = fields.Selection(SELECTION_1_6)
    store_gemini_overall_quality = fields.Selection(SELECTION_1_6)

    store_claude_truthfulness = fields.Selection(SELECTION_1_6)
    store_claude_instruction_following = fields.Selection(SELECTION_1_6)
    store_claude_writing_quality = fields.Selection(SELECTION_1_6)
    store_claude_verbosity = fields.Selection(SELECTION_1_6)
    store_claude_prompt_correctness = fields.Selection(SELECTION_1_6)
    store_claude_overall_quality = fields.Selection(SELECTION_1_6)
    # end

    # ──────────────────────────────────────────────────────────────
    # tooltip fields start
    # ──────────────────────────────────────────────────────────────
    reason1_gpt_truthfulness = fields.Text()
    reason1_gpt_instruction_following = fields.Text()
    reason1_gpt_writing_quality = fields.Text()
    reason1_gpt_verbosity = fields.Text()
    reason1_gpt_prompt_correctness = fields.Text()
    reason1_gpt_overall_quality = fields.Text()

    reason1_gemini_truthfulness = fields.Text()
    reason1_gemini_instruction_following = fields.Text()
    reason1_gemini_writing_quality = fields.Text()
    reason1_gemini_verbosity = fields.Text()
    reason1_gemini_prompt_correctness = fields.Text()
    reason1_gemini_overall_quality = fields.Text()

    reason1_claude_truthfulness = fields.Text()
    reason1_claude_instruction_following = fields.Text()
    reason1_claude_writing_quality = fields.Text()
    reason1_claude_verbosity = fields.Text()
    reason1_claude_prompt_correctness = fields.Text()
    reason1_claude_overall_quality = fields.Text()
    # end

    # ──────────────────────────────────────────────────────────────
    # indicator fields start
    # ──────────────────────────────────────────────────────────────
    error_gpt_truthfulness = fields.Boolean(default=False)
    error_gpt_instruction_following = fields.Boolean(default=False)
    error_gpt_writing_quality = fields.Boolean(default=False)
    error_gpt_verbosity = fields.Boolean(default=False)
    error_gpt_prompt_correctness = fields.Boolean(default=False)
    error_gpt_overall_quality = fields.Boolean(default=False)

    error_gemini_truthfulness = fields.Boolean(default=False)
    error_gemini_instruction_following = fields.Boolean(default=False)
    error_gemini_writing_quality = fields.Boolean(default=False)
    error_gemini_verbosity = fields.Boolean(default=False)
    error_gemini_prompt_correctness = fields.Boolean(default=False)
    error_gemini_overall_quality = fields.Boolean(default=False)

    error_claude_truthfulness = fields.Boolean(default=False)
    error_claude_instruction_following = fields.Boolean(default=False)
    error_claude_writing_quality = fields.Boolean(default=False)
    error_claude_verbosity = fields.Boolean(default=False)
    error_claude_prompt_correctness = fields.Boolean(default=False)
    error_claude_overall_quality = fields.Boolean(default=False)
    # end

    # ──────────────────────────────────────────────────────────────
    # _safe_write — retry on PostgreSQL serialization conflicts
    # ──────────────────────────────────────────────────────────────
    def _safe_write(self, vals, label=""):
        """Write values with retry on PostgreSQL serialization failures."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.env.cr.savepoint():
                    self.write(vals)
                return True
            except Exception as e:
                err_msg = str(e).lower()
                is_serialization = (
                    "could not serialize access" in err_msg
                    or "concurrent update" in err_msg
                    or "deadlock detected" in err_msg
                )
                if is_serialization and attempt < max_retries - 1:
                    wait = 0.5 * (2**attempt)
                    _logger.warning(
                        "Serialization conflict on record %s (%s), retry %d/%d in %.1fs: %s",
                        self.id,
                        label,
                        attempt + 1,
                        max_retries,
                        wait,
                        e,
                    )
                    _time_mod.sleep(wait)
                    self.invalidate_recordset()
                else:
                    raise
        return False

    # ──────────────────────────────────────────────────────────────
    # action_submit_prompt — button trigger for background eval
    # ──────────────────────────────────────────────────────────────
    def action_submit_prompt(self):
        if self.eval_status == "evaluating":
            _logger.warning(
                "Re-submitting record %s | task_id=%s while already evaluating (manual retry)",
                self.id,
                self.task_id or "",
            )

        # Clear all downstream fields
        dims = [
            "truthfulness",
            "instruction_following",
            "writing_quality",
            "verbosity",
            "prompt_correctness",
            "overall_quality",
        ]
        clear_vals = {
            "is_eval_done": False,
            "is_processed": False,
            "qc_task_status": False,
        }

        for model in ["gpt", "gemini", "claude"]:
            for d in dims:
                field = f"{model}_{d}"
                clear_vals[field] = False
                clear_vals[f"store_{field}"] = False
                clear_vals[f"reason1_{field}"] = False
                clear_vals[f"error_{field}"] = False

        for n in range(1, 6):
            clear_vals[f"rubric{n}_name"] = False
            clear_vals[f"store_rubric{n}_name"] = False
            clear_vals[f"error_rubric{n}_name"] = False
            clear_vals[f"reason1_rubric{n}_name"] = False
            for mk in ["gpt", "gemini", "claude"]:
                clear_vals[f"{mk}_rubric{n}_rating"] = False
                clear_vals[f"store_{mk}_rubric{n}_rating"] = False
                clear_vals[f"reason1_{mk}_rubric{n}_rating"] = False
                clear_vals[f"error_{mk}_rubric{n}_rating"] = False

        clear_vals["justification"] = False
        clear_vals["store_justification"] = False
        clear_vals["error_justification"] = False
        clear_vals["eval_status"] = "evaluating"
        self.write(clear_vals)

        record_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id
        needs_full_pipeline = not (
            self.gpt_response or self.gemini_response or self.claude_response
        )

        @self.env.cr.postcommit.add
        def _queue_eval():
            if needs_full_pipeline:
                _EVAL_POOL.submit(
                    _run_full_pipeline_background, db_name, record_id, notify_partner_id
                )
            else:
                _EVAL_POOL.submit(
                    _run_eval_background, db_name, record_id, notify_partner_id
                )

        _logger.info(
            "Eval queued for record %s | task_id=%s → background pool",
            self.id,
            self.task_id or "",
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Evaluation Started",
                "message": "Responses are being evaluated in the background. Results will appear automatically.",
                "type": "info",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    # ──────────────────────────────────────────────────────────────
    # eval_task — entry point for RabbitMQ consumer (called via XML-RPC)
    # ──────────────────────────────────────────────────────────────
    def eval_task(self):
        for rec in self:
            db_name = self.env.cr.dbname
            _run_full_pipeline_background(db_name, rec.id)
        return True

    # ──────────────────────────────────────────────────────────────
    # run_qc_checks
    # ──────────────────────────────────────────────────────────────
    def run_qc_checks(self):
        """Run QC checks on this record's justification."""
        if not self.justification:
            _logger.info("Skipping QC for record %s — no justification", self.id)
            return
        api_key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        if not api_key:
            _logger.warning("Skipping QC for record %s — no API key", self.id)
            return
        try:
            _llm_mod.set_usage_context(berserker_id=self.id, call_type="qc")
            qc_result = perform_qc_checks_kimi(
                api_key=api_key,
                prompt=self.client_prompt or "",
                gpt_response=self.gpt_response or "",
                gemini_response=self.gemini_response or "",
                claude_response=self.claude_response or "",
                justification=self.justification or "",
            )
            qc_status = qc_result.get("qc_status", "fail")
            self.write(
                {
                    "qc_task_status": "pass" if qc_status == "pass" else "fail",
                }
            )
        except Exception as e:
            _logger.error(
                "QC check failed for record %s: %s", self.id, e, exc_info=True
            )

    # ──────────────────────────────────────────────────────────────
    # evaluate_task
    # ──────────────────────────────────────────────────────────────
    def evaluate_task(self):
        # GPT ratings
        if self.gpt_truthfulness and self.store_gpt_truthfulness:
            self.error_gpt_truthfulness = check_error(
                int(self.gpt_truthfulness), int(self.store_gpt_truthfulness)
            )
        if self.gpt_instruction_following and self.store_gpt_instruction_following:
            self.error_gpt_instruction_following = check_error(
                int(self.gpt_instruction_following),
                int(self.store_gpt_instruction_following),
            )
        if self.gpt_writing_quality and self.store_gpt_writing_quality:
            self.error_gpt_writing_quality = check_error(
                int(self.gpt_writing_quality), int(self.store_gpt_writing_quality)
            )
        if self.gpt_verbosity and self.store_gpt_verbosity:
            self.error_gpt_verbosity = check_error(
                int(self.gpt_verbosity), int(self.store_gpt_verbosity)
            )
        if self.gpt_prompt_correctness and self.store_gpt_prompt_correctness:
            self.error_gpt_prompt_correctness = check_error(
                int(self.gpt_prompt_correctness),
                int(self.store_gpt_prompt_correctness),
            )
        if self.gpt_overall_quality and self.store_gpt_overall_quality:
            self.error_gpt_overall_quality = check_error(
                int(self.gpt_overall_quality), int(self.store_gpt_overall_quality)
            )

        # Gemini ratings
        if self.gemini_truthfulness and self.store_gemini_truthfulness:
            self.error_gemini_truthfulness = check_error(
                int(self.gemini_truthfulness), int(self.store_gemini_truthfulness)
            )
        if (
            self.gemini_instruction_following
            and self.store_gemini_instruction_following
        ):
            self.error_gemini_instruction_following = check_error(
                int(self.gemini_instruction_following),
                int(self.store_gemini_instruction_following),
            )
        if self.gemini_writing_quality and self.store_gemini_writing_quality:
            self.error_gemini_writing_quality = check_error(
                int(self.gemini_writing_quality),
                int(self.store_gemini_writing_quality),
            )
        if self.gemini_verbosity and self.store_gemini_verbosity:
            self.error_gemini_verbosity = check_error(
                int(self.gemini_verbosity), int(self.store_gemini_verbosity)
            )
        if self.gemini_prompt_correctness and self.store_gemini_prompt_correctness:
            self.error_gemini_prompt_correctness = check_error(
                int(self.gemini_prompt_correctness),
                int(self.store_gemini_prompt_correctness),
            )
        if self.gemini_overall_quality and self.store_gemini_overall_quality:
            self.error_gemini_overall_quality = check_error(
                int(self.gemini_overall_quality),
                int(self.store_gemini_overall_quality),
            )

        # Claude ratings
        if self.claude_truthfulness and self.store_claude_truthfulness:
            self.error_claude_truthfulness = check_error(
                int(self.claude_truthfulness), int(self.store_claude_truthfulness)
            )
        if (
            self.claude_instruction_following
            and self.store_claude_instruction_following
        ):
            self.error_claude_instruction_following = check_error(
                int(self.claude_instruction_following),
                int(self.store_claude_instruction_following),
            )
        if self.claude_writing_quality and self.store_claude_writing_quality:
            self.error_claude_writing_quality = check_error(
                int(self.claude_writing_quality),
                int(self.store_claude_writing_quality),
            )
        if self.claude_verbosity and self.store_claude_verbosity:
            self.error_claude_verbosity = check_error(
                int(self.claude_verbosity), int(self.store_claude_verbosity)
            )
        if self.claude_prompt_correctness and self.store_claude_prompt_correctness:
            self.error_claude_prompt_correctness = check_error(
                int(self.claude_prompt_correctness),
                int(self.store_claude_prompt_correctness),
            )
        if self.claude_overall_quality and self.store_claude_overall_quality:
            self.error_claude_overall_quality = check_error(
                int(self.claude_overall_quality),
                int(self.store_claude_overall_quality),
            )

        # Justification unchanged check
        if self.justification and self.store_justification:
            self.error_justification = self.justification == self.store_justification

        # Rubric rating comparison
        for model in ["gpt", "gemini", "claude"]:
            for n in range(1, 6):
                human_field = f"{model}_rubric{n}_rating"
                store_field = f"store_{model}_rubric{n}_rating"
                error_field = f"error_{model}_rubric{n}_rating"
                human_val = getattr(self, human_field, "") or ""
                store_val = getattr(self, store_field, "") or ""
                if human_val and store_val:
                    setattr(
                        self, error_field, check_error(int(human_val), int(store_val))
                    )

        # Rubric name unchanged check
        for n in range(1, 6):
            rname = getattr(self, f"rubric{n}_name", "") or ""
            store_rname = getattr(self, f"store_rubric{n}_name", "") or ""
            if rname and store_rname and rname == store_rname:
                setattr(self, f"error_rubric{n}_name", True)
                setattr(
                    self,
                    f"reason1_rubric{n}_name",
                    "Rubric name unchanged from AI-generated value.",
                )

    # ──────────────────────────────────────────────────────────────
    # submit_task
    # ──────────────────────────────────────────────────────────────
    def submit_task(self):
        self.task_status = "Submitted"
        self.submitted_at = fields.Datetime.now()
        return True
