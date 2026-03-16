# -*- coding: utf-8 -*-
import logging
import threading
import time as _time_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from odoo import models, fields, api
from odoo.modules.registry import Registry
from odoo.exceptions import ValidationError
from ..controllers import llm_actions
from ..services.rabbitmq_service import (
    publish_eval_task,
    publish_qc_task,
    batch_publish_reeval_tasks,
)
import json
import re
import boto3
import io
from datetime import datetime, timezone
import os
import requests
from dotenv import load_dotenv

try:
    import psycopg2
except ImportError:
    psycopg2 = None

load_dotenv()

_logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph-genai.facebook.com/v24.0"
WORKSTREAM = "vendor_onboarding"


def check_error(value, value2):
    minus_val = value - 1
    plus_val = value + 1
    check = True if value2 > plus_val or value2 < minus_val else False
    return check


def _strip_reasoning_tags(text: str) -> str:
    """Strip model reasoning tags from response text.

    Models may include reasoning wrapped in |<reasoning_start>|...|<reasoning_end>|
    tags. Only the content after the reasoning block should be displayed.
    """
    if not text:
        return text
    stripped = re.sub(
        r"\|<reasoning_start>\|.*?\|<reasoning_end>\|\s*", "", text, flags=re.DOTALL
    )
    return stripped.strip()


def _safe_get(data, *keys, default=""):
    """Safely traverse nested dicts. Returns default if any key is missing or value is falsy."""
    current = data
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return default
        current = current[k]
    return current if current else default


def _to_selection_score(raw, min_val, max_val):
    """Convert a raw API score to an integer string for Selection fields.

    Returns '' for None, empty, out-of-range, or unparseable values
    so Odoo never receives invalid Selection keys.
    """
    if raw is None or raw == "":
        return ""
    try:
        val = int(float(raw))
        if min_val <= val <= max_val:
            return str(val)
        return ""
    except (ValueError, TypeError):
        return ""


def _extract_eval_scores(resp, response_key="response_a"):
    """Extract all 6 dimension scores/reasons + overall from an evaluation_result entry.

    Args:
        resp: Single item from evaluation_for_tasks_sync output.
        response_key: 'response_a' or 'response_b'.

    Returns:
        dict with keys like 'truthfulness', 'instruction_following', etc.,
        each containing 'score' (str) and 'reason' (str), plus
        'overall_quality' with 'score' and 'reason'.
    """
    er = resp.get("evaluation_result", {}).get(response_key, {}) if resp else {}
    dim_map = {
        "truthfulness": "truthfulness",
        "instruction_following": "instruction_following",
        "writing_quality": "writing_style",
        "verbosity": "verbosity",
        "prompt_correctness": "prompt_correctness",
    }
    result = {}
    for field_name, api_key in dim_map.items():
        dim = er.get(api_key, {})
        result[field_name] = {
            "score": _to_selection_score(dim.get("score"), 1, 6),
            "reason": str(dim.get("reason", "")) if dim.get("reason") else "",
        }
    oq = er.get("overall_quality", {})
    result["overall_quality"] = {
        "score": _to_selection_score(oq.get("weighted_score"), 1, 6),
        "reason": str(oq.get("reason", "")) if oq.get("reason") else "",
    }
    return result


def _extract_comparison(resp, comparison_key="comparison_ab"):
    """Extract comparison_score and overall_comment/comparison_comment from a response."""
    comp = resp.get(comparison_key, {}) if resp else {}
    score_val = comp.get("comparison_score")
    comment_key = (
        "overall_comment" if comparison_key == "comparison_ab" else "comparison_comment"
    )
    comment_val = comp.get(comment_key, "")
    return {
        "score": _to_selection_score(score_val, -3, 3),
        "comment": str(comment_val) if comment_val else "",
    }


class PreferenceRankingEnhancementCriteria(models.Model):
    """A single prompt-enhancement criterion linked to a PreferenceRanking record.

    Users can add, edit, and delete criteria inline from the form view.
    Each criterion is a free-text description of one enhancement rule/constraint.
    """

    _name = "preference.ranking.enhancement.criteria"
    _description = "Prompt Enhancement Criteria"
    _order = "sequence, id"

    ranking_id = fields.Many2one(
        "preference.ranking",
        string="Preference Ranking",
        required=True,
        ondelete="cascade",
    )
    sequence = fields.Integer(
        string="Sequence",
        default=10,
        help="Order in which this criterion is displayed.",
    )
    name = fields.Char(
        string="Criteria",
        required=True,
        help="A single enhancement criterion or constraint for prompt improvement.",
    )


class PreferenceRanking(models.Model):
    _name = "preference.ranking"
    _description = "Preference Ranking"
    _rec_name = "task_id"

    def _compute_is_tasker(self):
        has_group = self.env.user.has_group("preference_ranking.group_vindex_tasker")
        for record in self:
            record.is_tasker = has_group

    def action_upload_jsonl(self):
        data = []
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for record in self:
            if record.qc_task_status == "pass":
                data.append(
                    {
                        "evaluation_id": f"eval_sxs_2026_{record.task_id}",
                        "randomized_responses_order": record.is_randomized,
                        "prompt_metadata": {
                            "dialog_history": [
                                {"role": "user", "content": record.client_prompt}
                            ],
                            "response_a": record.client_response_a,
                            "response_b": record.client_response_b,
                        },
                        "external_model_responses": {
                            "gpt_5_2": record.gpt_response
                            if record.gpt_response
                            else "",
                            "gemini_3_pro": record.gemini_response
                            if record.gemini_response
                            else "",
                        },
                        "ratings_ab": [
                            {
                                "rater_id": record.task_id,
                                "timestamp": timestamp,
                                "prompt_requires_fresh_info": False,
                                "ab_preference": int(record.ab_preference)
                                if record.ab_preference
                                else False,
                                "ab_comment": record.ab_comment
                                if record.ab_comment
                                else "",
                                "pointwise_evaluations": {
                                    "response_a": {
                                        "truthfulness": int(record.truthfulness_a)
                                        if record.truthfulness_a
                                        else False,
                                        "instruction_following": int(
                                            record.instruction_following_a
                                        )
                                        if record.instruction_following_a
                                        else False,
                                        "writing_quality": int(record.writing_quality_a)
                                        if record.writing_quality_a
                                        else False,
                                        "verbosity": int(record.verbosity_a)
                                        if record.verbosity_a
                                        else False,
                                        "correctness": int(record.prompt_correctness_a)
                                        if record.prompt_correctness_a
                                        else False,
                                        "overall_quality": int(record.overall_quality_a)
                                        if record.overall_quality_a
                                        else False,
                                    },
                                    "response_b": {
                                        "truthfulness": int(record.truthfulness_b)
                                        if record.truthfulness_b
                                        else False,
                                        "instruction_following": int(
                                            record.instruction_following_b
                                        )
                                        if record.instruction_following_b
                                        else False,
                                        "writing_quality": int(record.writing_quality_b)
                                        if record.writing_quality_b
                                        else False,
                                        "verbosity": int(record.verbosity_b)
                                        if record.verbosity_b
                                        else False,
                                        "correctness": int(record.prompt_correctness_b)
                                        if record.prompt_correctness_b
                                        else False,
                                        "overall_quality": int(record.overall_quality_b)
                                        if record.overall_quality_b
                                        else False,
                                    },
                                },
                                "external_model_comparisons": {
                                    "gpt_5_2": {
                                        "ab_gpt_preference": int(
                                            record.ab_gpt_preference
                                        )
                                        if record.ab_gpt_preference
                                        else False,
                                        "ab_gpt_comment": record.ab_gpt_comment
                                        if record.ab_gpt_comment
                                        else "",
                                    },
                                    "gemini_3_pro": {
                                        "ab_gemini_preference": int(
                                            record.ab_gemini_preference
                                        )
                                        if record.ab_gemini_preference
                                        else False,
                                        "ab_gemini_comment": record.ab_gemini_comment
                                        if record.ab_gemini_comment
                                        else "",
                                    },
                                },
                                "rubrics": [
                                    {
                                        "name": record.gpt_rubric_name
                                        if record.gpt_rubric_name
                                        else "",
                                        "description": record.gpt_rubric_description
                                        if record.gpt_rubric_description
                                        else "",
                                        "scale": int(record.gpt_rubric_scale_rating)
                                        if record.gpt_rubric_scale_rating
                                        else False,
                                    },
                                    {
                                        "name": record.gemini_rubric_name
                                        if record.gemini_rubric_name
                                        else "",
                                        "description": record.gemini_rubric_description
                                        if record.gemini_rubric_description
                                        else "",
                                        "scale": int(record.gemini_rubric_scale_rating)
                                        if record.gemini_rubric_scale_rating
                                        else False,
                                    },
                                ],
                            }
                        ],
                    }
                )
        if not data:
            raise ValidationError("No submitted records found.")
        jsonl_output = io.StringIO()
        for entry in data:
            json.dump(entry, jsonl_output)
            jsonl_output.write("\n")

        content = jsonl_output.getvalue().encode("utf-8")

        access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        s3 = boto3.client(
            "s3", aws_access_key_id=access_key_id, aws_secret_access_key=secret_key
        )
        s3_key = f"delivery/{timestamp}_delivery.jsonl"

        s3.put_object(
            Bucket="prod-grtlabs",
            Key=s3_key,
            Body=content,
            ContentType="application/x-jsonlines",
        )

    # def _trigger_universal_background_task(self, gemini_api_key, openai_api_key):
    #     record_ids = self.ids
    #     model_name = self._name
    #     user_id = 3 or self.env.uid
    #     context = dict(self.env.context)
    #     db_name = self.env.cr.dbname
    #     def background_worker():
    #         with registry(db_name).cursor() as new_cr:
    #             try:
    #                 new_env = api.Environment(new_cr, user_id, context)
    #                 records = new_env[model_name].browse(record_ids)
    #                 for i in records:
    #                     rejection_reason = ''
    #                     rejection_status = 'ACCEPT'
    #                     list1 = [i.client_prompt]
    #                     tasks_response = llm_actions.prompt_rejection_check_sync(gemini_api_key=gemini_api_key, user_prompts=list1)
    #                     if tasks_response:
    #                         _logger.info('tasks1_response---------------------------%s', tasks_response)
    #                         if 'status' in tasks_response[0] and tasks_response[0]['status'] != 'ACCEPT':
    #                             rejection_reason = tasks_response[0]['result']['reason']
    #                             i.sudo().write({
    #                                 'prompt_rejection_reason': rejection_reason,
    #                                 'is_processed': True,
    #                                 'is_ratable': False
    #                             })
    #                     if rejection_status == 'ACCEPT':
    #                         list2 = [{
    #                             'task_id': i.task_id,
    #                             'prompt': i.client_prompt
    #                         }]
    #                         tasks2_response = llm_actions.response_generation_for_tasks_sync(gemini_api_key=gemini_api_key,
    #                                                                              openai_api_key=openai_api_key,
    #                                                                              tasks=list2)
    #                         if tasks2_response:
    #                             _logger.info('tasks2_response---------------------------%s', tasks2_response)
    #                             gemini_response = tasks2_response[0]['gemini_response']
    #                             gpt_response = tasks2_response[0]['gpt_response']
    #                             i.sudo().write({
    #                                 'is_ratable': True,
    #                                 'is_processed': True,
    #                                 'gemini_response': gemini_response,
    #                                 'gpt_response': gpt_response,
    #                                 'prompt_rejection_reason': rejection_reason
    #                             })
    #                 new_cr.commit()
    #             except Exception as e:
    #                 _logger.info(f"{e}")
    #     _logger.info(f"{record_ids}{model_name}--{user_id}--{context}---{db_name} Background Task Engine started")
    #     threading.Thread(target=background_worker, daemon=True).start()
    #     _logger.info(f"{record_ids}{model_name}--{user_id}--{context}---{db_name} Background Task Engine End")

    def _call_generation(self, model_name, dialog_id, user_prompt, genai_api_key):
        url = f"{GRAPH_BASE_URL}/llm_annotations_metagen_stream_turn"

        payload = {
            "access_token": genai_api_key,
            "dialog": {
                "messages": [
                    {
                        "source": {"role": "user"},
                        "contents": [{"text": {"text": user_prompt}}],
                        "is_end_of_turn": True,
                        "is_complete": True,
                    }
                ]
            },
            "workstream": WORKSTREAM,
            "model": model_name,
            "dialog_id": dialog_id,
        }

        response = requests.post(url, json=payload)
        response.raise_for_status()

        # Response is streamed (multiple JSON objects per line),
        # parse the last one containing dialog_candidates
        data = None
        for line in reversed(response.text.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if "dialog_candidates" in parsed:
                    data = parsed
                    break
            except json.JSONDecodeError:
                continue

        if data is None:
            raise ValueError(
                "No valid response with dialog_candidates found from GenAI API"
            )

        return data

    def _trigger_universal_background_task(
        self, genai_api_key, openai_api_key, dialog_id, model_1, model_2
    ):
        record_ids = self.ids
        model_name = self._name
        user_id = 3 or self.env.uid
        context = dict(self.env.context)
        db_name = self.env.cr.dbname

        def background_worker():
            with Registry(db_name).cursor() as new_cr:
                try:
                    new_env = api.Environment(new_cr, user_id, context)
                    records = new_env[model_name].browse(record_ids)
                    for i in records:
                        response_1 = self._call_generation(
                            model_1, dialog_id, i.client_prompt, genai_api_key
                        )
                        client_response_a = (
                            response_1["dialog_candidates"][0]["dialog"]["messages"][0][
                                "contents"
                            ][0]["text"]["text"]
                            if response_1
                            and "dialog_candidates" in response_1
                            and response_1["dialog_candidates"]
                            and "dialog" in response_1["dialog_candidates"][0]
                            and "messages"
                            in response_1["dialog_candidates"][0]["dialog"]
                            and response_1["dialog_candidates"][0]["dialog"]["messages"]
                            and "contents"
                            in response_1["dialog_candidates"][0]["dialog"]["messages"][
                                0
                            ]
                            and response_1["dialog_candidates"][0]["dialog"][
                                "messages"
                            ][0]["contents"]
                            and "text"
                            in response_1["dialog_candidates"][0]["dialog"]["messages"][
                                0
                            ]["contents"][0]
                            and "text"
                            in response_1["dialog_candidates"][0]["dialog"]["messages"][
                                0
                            ]["contents"][0]["text"]
                            and response_1["dialog_candidates"][0]["dialog"][
                                "messages"
                            ][0]["contents"][0]["text"]["text"]
                            else ""
                        )
                        _logger.info(
                            "client_reponse_a---------------------------%s",
                            client_response_a,
                        )

                        response_2 = self._call_generation(
                            model_2, dialog_id, i.client_prompt, genai_api_key
                        )
                        client_response_b = (
                            response_2["dialog_candidates"][0]["dialog"]["messages"][0][
                                "contents"
                            ][0]["text"]["text"]
                            if response_2
                            and "dialog_candidates" in response_2
                            and response_2["dialog_candidates"]
                            and "dialog" in response_2["dialog_candidates"][0]
                            and "messages"
                            in response_2["dialog_candidates"][0]["dialog"]
                            and response_2["dialog_candidates"][0]["dialog"]["messages"]
                            and "contents"
                            in response_2["dialog_candidates"][0]["dialog"]["messages"][
                                0
                            ]
                            and response_2["dialog_candidates"][0]["dialog"][
                                "messages"
                            ][0]["contents"]
                            and "text"
                            in response_2["dialog_candidates"][0]["dialog"]["messages"][
                                0
                            ]["contents"][0]
                            and "text"
                            in response_2["dialog_candidates"][0]["dialog"]["messages"][
                                0
                            ]["contents"][0]["text"]
                            and response_2["dialog_candidates"][0]["dialog"][
                                "messages"
                            ][0]["contents"][0]["text"]["text"]
                            else ""
                        )
                        _logger.info(
                            "client_reponse_b---------------------------%s",
                            client_response_b,
                        )
                        list2 = [{"task_id": i.task_id, "prompt": i.client_prompt}]
                        tasks2_response = (
                            llm_actions.response_generation_for_tasks_sync(
                                openai_api_key=openai_api_key,
                                tasks=list2,
                            )
                        )
                        gemini_response = ""
                        gpt_response = ""
                        if tasks2_response:
                            _logger.info(
                                "tasks2_response---------------------------%s",
                                tasks2_response,
                            )
                            gemini_response = tasks2_response[0]["gemini_response"]
                            gpt_response = tasks2_response[0]["gpt_response"]
                        is_processed = True
                        if (
                            "error: 500 Server Error" in gemini_response
                            or "error: 429 Client Error" in gemini_response
                        ):
                            is_processed = False
                        i.sudo().write(
                            {
                                "is_ratable": True,
                                "is_processed": is_processed,
                                "gemini_response": gemini_response,
                                "gpt_response": gpt_response,
                                "client_response_a": client_response_a,
                                "client_response_b": client_response_b,
                            }
                        )
                    new_cr.commit()
                except Exception as e:
                    _logger.info(f"{e}")

        _logger.info(
            f"{record_ids}{model_name}--{user_id}--{context}---{db_name} Background Task Engine started"
        )
        threading.Thread(target=background_worker, daemon=True).start()
        _logger.info(
            f"{record_ids}{model_name}--{user_id}--{context}---{db_name} Background Task Engine End"
        )

    task_id = fields.Char()
    enhancement_criteria_ids = fields.One2many(
        "preference.ranking.enhancement.criteria",
        "ranking_id",
        string="Prompt Enhancement Criterias",
        help="List of enhancement criteria/constraints for this task's prompt.",
    )
    task_status = fields.Selection(
        [("Submitted", "Submitted"), ("NotSubmitted", "Not Submitted")]
    )
    employee_id = fields.Many2one("hr.employee")
    user_id = fields.Many2one(related="employee_id.user_id")
    is_ratable = fields.Boolean()
    is_processed = fields.Boolean()
    is_eval_done = fields.Boolean()
    is_randomized = fields.Boolean()
    prompt_rejection_reason = fields.Text()
    rejection_reason = fields.Selection(
        [
            ("Image Handling", "Image Handling"),
            ("Missing Reference Text", "Missing Reference Text"),
            ("Safety Concerns", "Safety Concerns"),
            ("Gibberish / Nonsensical Content", "Gibberish / Nonsensical Content"),
            (
                "Contains Personal Identifiable Information",
                "Contains Personal Identifiable Information",
            ),
            (
                "Requires Localized or Real-Time Info",
                "Requires Localized or Real-Time Info",
            ),
            ("Identity Requests", "Identity Requests"),
            (
                "Prompt is In A Foreign (non-English) Language",
                "Prompt is In A Foreign (non-English) Language",
            ),
        ]
    )
    qc_task_status = fields.Selection([("pass", "Pass"), ("fail", "Fail")])
    submitted_at = fields.Datetime(string="Submitted At")
    client_prompt = fields.Text()
    client_response_a = fields.Text()
    client_response_b = fields.Text()
    enhance_prompt = fields.Text()
    ophelia_response_a = fields.Text()
    ophelia_reasoning = fields.Text(
        string="Ophelia Reasoning",
        help="Internal chain-of-thought / reasoning trace produced by the Ophelia model alongside its response. Backend use only.",
    )
    opalite_response_b = fields.Text()
    opalite_reasoning = fields.Text(
        string="Opalite Reasoning",
        help="Internal chain-of-thought / reasoning trace produced by the Opalite model alongside its response. Backend use only.",
    )

    client_response_a_display = fields.Text(
        compute="_compute_display_responses",
        store=False,
    )
    client_response_b_display = fields.Text(
        compute="_compute_display_responses",
        store=False,
    )
    ophelia_response_a_display = fields.Text(
        compute="_compute_display_responses",
        store=False,
    )
    opalite_response_b_display = fields.Text(
        compute="_compute_display_responses",
        store=False,
    )

    @api.depends(
        "client_response_a",
        "client_response_b",
        "ophelia_response_a",
        "opalite_response_b",
    )
    def _compute_display_responses(self):
        for rec in self:
            rec.client_response_a_display = _strip_reasoning_tags(
                rec.client_response_a or ""
            )
            rec.client_response_b_display = _strip_reasoning_tags(
                rec.client_response_b or ""
            )
            rec.ophelia_response_a_display = _strip_reasoning_tags(
                rec.ophelia_response_a or ""
            )
            rec.opalite_response_b_display = _strip_reasoning_tags(
                rec.opalite_response_b or ""
            )

    truthfulness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    instruction_following_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    writing_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    verbosity_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    prompt_correctness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    overall_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    truthfulness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    instruction_following_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    writing_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    verbosity_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    prompt_correctness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    overall_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ab_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    ab_comment = fields.Text()
    ophelia_truthfulness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ophelia_instruction_following_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ophelia_writing_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ophelia_verbosity_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ophelia_prompt_correctness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ophelia_overall_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_truthfulness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_instruction_following_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_writing_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_verbosity_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_prompt_correctness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_overall_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    enhance_ab_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    enhance_ab_comment = fields.Text()
    gpt_truthfulness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_instruction_following_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_writing_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_verbosity_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_prompt_correctness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_overall_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_truthfulness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_instruction_following_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_writing_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_verbosity_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_prompt_correctness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_overall_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpts_ab_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    gpts_ab_comment = fields.Text()
    geminis_ab_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    geminis_ab_comment = fields.Text()
    # fields to store in db only start
    store_truthfulness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_instruction_following_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_writing_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_verbosity_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_prompt_correctness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_overall_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_truthfulness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_instruction_following_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_writing_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_verbosity_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_prompt_correctness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_overall_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ab_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    store_ab_comment = fields.Text()
    store_ophelia_truthfulness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ophelia_instruction_following_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ophelia_writing_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ophelia_verbosity_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ophelia_prompt_correctness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ophelia_overall_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_truthfulness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_instruction_following_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_writing_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_verbosity_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_prompt_correctness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_overall_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_enhance_ab_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    store_enhance_ab_comment = fields.Text()
    store_gpt_truthfulness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_instruction_following_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_writing_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_verbosity_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_prompt_correctness_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_overall_quality_a = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_truthfulness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_instruction_following_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_writing_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_verbosity_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_prompt_correctness_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_overall_quality_b = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpts_ab_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    store_gpts_ab_comment = fields.Text()
    store_geminis_ab_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    store_geminis_ab_comment = fields.Text()
    # end
    # tooltip fields start
    reason1_truthfulness_a = fields.Text()
    reason1_instruction_following_a = fields.Text()
    reason1_writing_quality_a = fields.Text()
    reason1_verbosity_a = fields.Text()
    reason1_prompt_correctness_a = fields.Text()
    reason1_overall_quality_a = fields.Text()
    reason1_truthfulness_b = fields.Text()
    reason1_instruction_following_b = fields.Text()
    reason1_writing_quality_b = fields.Text()
    reason1_verbosity_b = fields.Text()
    reason1_prompt_correctness_b = fields.Text()
    reason1_overall_quality_b = fields.Text()
    reason1_ab_preference = fields.Text()
    reason1_ab_comment = fields.Text()
    reason1_ophelia_truthfulness_a = fields.Text()
    reason1_ophelia_instruction_following_a = fields.Text()
    reason1_ophelia_writing_quality_a = fields.Text()
    reason1_ophelia_verbosity_a = fields.Text()
    reason1_ophelia_prompt_correctness_a = fields.Text()
    reason1_ophelia_overall_quality_a = fields.Text()
    reason1_opalite_truthfulness_b = fields.Text()
    reason1_opalite_instruction_following_b = fields.Text()
    reason1_opalite_writing_quality_b = fields.Text()
    reason1_opalite_verbosity_b = fields.Text()
    reason1_opalite_prompt_correctness_b = fields.Text()
    reason1_opalite_overall_quality_b = fields.Text()
    reason1_enhance_ab_preference = fields.Text()
    reason1_enhance_ab_comment = fields.Text()
    reason1_gpt_truthfulness_a = fields.Text()
    reason1_gpt_instruction_following_a = fields.Text()
    reason1_gpt_writing_quality_a = fields.Text()
    reason1_gpt_verbosity_a = fields.Text()
    reason1_gpt_prompt_correctness_a = fields.Text()
    reason1_gpt_overall_quality_a = fields.Text()
    reason1_gemini_truthfulness_b = fields.Text()
    reason1_gemini_instruction_following_b = fields.Text()
    reason1_gemini_writing_quality_b = fields.Text()
    reason1_gemini_verbosity_b = fields.Text()
    reason1_gemini_prompt_correctness_b = fields.Text()
    reason1_gemini_overall_quality_b = fields.Text()
    reason1_gpts_ab_preference = fields.Text()
    reason1_gpts_ab_comment = fields.Text()
    reason1_geminis_ab_preference = fields.Text()
    reason1_geminis_ab_comment = fields.Text()
    # end
    # indicator fields start
    error_truthfulness_a = fields.Boolean(default=False)
    error_instruction_following_a = fields.Boolean(default=False)
    error_writing_quality_a = fields.Boolean(default=False)
    error_verbosity_a = fields.Boolean(default=False)
    error_prompt_correctness_a = fields.Boolean(default=False)
    error_overall_quality_a = fields.Boolean(default=False)
    error_truthfulness_b = fields.Boolean(default=False)
    error_instruction_following_b = fields.Boolean(default=False)
    error_writing_quality_b = fields.Boolean(default=False)
    error_verbosity_b = fields.Boolean(default=False)
    error_prompt_correctness_b = fields.Boolean(default=False)
    error_overall_quality_b = fields.Boolean(default=False)
    error_ab_preference = fields.Boolean(default=False)
    error_ab_comment = fields.Boolean(default=False)
    error_ophelia_truthfulness_a = fields.Boolean(default=False)
    error_ophelia_instruction_following_a = fields.Boolean(default=False)
    error_ophelia_writing_quality_a = fields.Boolean(default=False)
    error_ophelia_verbosity_a = fields.Boolean(default=False)
    error_ophelia_prompt_correctness_a = fields.Boolean(default=False)
    error_ophelia_overall_quality_a = fields.Boolean(default=False)
    error_opalite_truthfulness_b = fields.Boolean(default=False)
    error_opalite_instruction_following_b = fields.Boolean(default=False)
    error_opalite_writing_quality_b = fields.Boolean(default=False)
    error_opalite_verbosity_b = fields.Boolean(default=False)
    error_opalite_prompt_correctness_b = fields.Boolean(default=False)
    error_opalite_overall_quality_b = fields.Boolean(default=False)
    error_enhance_ab_preference = fields.Boolean(default=False)
    error_enhance_ab_comment = fields.Boolean(default=False)
    error_gpt_truthfulness_a = fields.Boolean(default=False)
    error_gpt_instruction_following_a = fields.Boolean(default=False)
    error_gpt_writing_quality_a = fields.Boolean(default=False)
    error_gpt_verbosity_a = fields.Boolean(default=False)
    error_gpt_prompt_correctness_a = fields.Boolean(default=False)
    error_gpt_overall_quality_a = fields.Boolean(default=False)
    error_gemini_truthfulness_b = fields.Boolean(default=False)
    error_gemini_instruction_following_b = fields.Boolean(default=False)
    error_gemini_writing_quality_b = fields.Boolean(default=False)
    error_gemini_verbosity_b = fields.Boolean(default=False)
    error_gemini_prompt_correctness_b = fields.Boolean(default=False)
    error_gemini_overall_quality_b = fields.Boolean(default=False)
    error_gpts_ab_preference = fields.Boolean(default=False)
    error_gpts_ab_comment = fields.Boolean(default=False)
    error_geminis_ab_preference = fields.Boolean(default=False)
    error_geminis_ab_comment = fields.Boolean(default=False)
    # end

    rubric1_name = fields.Text()
    rubric1_description = fields.Text()
    rubric2_name = fields.Text()
    rubric2_description = fields.Text()
    rubric3_name = fields.Text()
    rubric3_description = fields.Text()
    rubric4_name = fields.Text()
    rubric4_description = fields.Text()
    rubric5_name = fields.Text()
    rubric5_description = fields.Text()
    ophelia_rubric1_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ophelia_rubric2_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ophelia_rubric3_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ophelia_rubric4_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    ophelia_rubric5_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_rubric1_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_rubric2_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_rubric3_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_rubric4_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    opalite_rubric5_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    gpt_comment = fields.Text()
    gemini_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    gemini_comment = fields.Text()
    gpt_rubric1_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_rubric2_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_rubric3_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_rubric4_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gpt_rubric5_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_rubric1_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_rubric2_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_rubric3_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_rubric4_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    gemini_rubric5_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_rubric1_name = fields.Text()
    store_rubric1_description = fields.Text()
    store_rubric2_name = fields.Text()
    store_rubric2_description = fields.Text()
    store_rubric3_name = fields.Text()
    store_rubric3_description = fields.Text()
    store_rubric4_name = fields.Text()
    store_rubric4_description = fields.Text()
    store_rubric5_name = fields.Text()
    store_rubric5_description = fields.Text()
    store_ophelia_rubric1_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ophelia_rubric2_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ophelia_rubric3_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ophelia_rubric4_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_ophelia_rubric5_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_rubric1_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_rubric2_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_rubric3_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_rubric4_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_opalite_rubric5_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_rubric1_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_rubric2_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_rubric3_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_rubric4_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_rubric5_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_rubric1_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_rubric2_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_rubric3_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_rubric4_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gemini_rubric5_rating = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6")]
    )
    store_gpt_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    store_gpt_comment = fields.Text()
    store_gemini_preference = fields.Selection(
        [
            ("-3", "-3"),
            ("-2", "-2"),
            ("-1", "-1"),
            ("0", "0"),
            ("1", "1"),
            ("2", "2"),
            ("3", "3"),
        ]
    )
    store_gemini_comment = fields.Text()
    reason1_rubric1_name = fields.Text()
    reason1_rubric1_description = fields.Text()
    reason1_rubric2_name = fields.Text()
    reason1_rubric2_description = fields.Text()
    reason1_rubric3_name = fields.Text()
    reason1_rubric3_description = fields.Text()
    reason1_rubric4_name = fields.Text()
    reason1_rubric4_description = fields.Text()
    reason1_rubric5_name = fields.Text()
    reason1_rubric5_description = fields.Text()
    reason1_ophelia_rubric1_rating = fields.Text()
    reason1_ophelia_rubric2_rating = fields.Text()
    reason1_ophelia_rubric3_rating = fields.Text()
    reason1_ophelia_rubric4_rating = fields.Text()
    reason1_ophelia_rubric5_rating = fields.Text()
    reason1_opalite_rubric1_rating = fields.Text()
    reason1_opalite_rubric2_rating = fields.Text()
    reason1_opalite_rubric3_rating = fields.Text()
    reason1_opalite_rubric4_rating = fields.Text()
    reason1_opalite_rubric5_rating = fields.Text()
    reason1_gpt_rubric1_rating = fields.Text()
    reason1_gpt_rubric2_rating = fields.Text()
    reason1_gpt_rubric3_rating = fields.Text()
    reason1_gpt_rubric4_rating = fields.Text()
    reason1_gpt_rubric5_rating = fields.Text()
    reason1_gemini_rubric1_rating = fields.Text()
    reason1_gemini_rubric2_rating = fields.Text()
    reason1_gemini_rubric3_rating = fields.Text()
    reason1_gemini_rubric4_rating = fields.Text()
    reason1_gemini_rubric5_rating = fields.Text()
    reason1_gpt_preference = fields.Text()
    reason1_gpt_comment = fields.Text()
    reason1_gemini_preference = fields.Text()
    reason1_gemini_comment = fields.Text()
    error_rubric1_name = fields.Boolean(default=False)
    error_rubric1_description = fields.Boolean(default=False)
    error_rubric2_name = fields.Boolean(default=False)
    error_rubric2_description = fields.Boolean(default=False)
    error_rubric3_name = fields.Boolean(default=False)
    error_rubric3_description = fields.Boolean(default=False)
    error_rubric4_name = fields.Boolean(default=False)
    error_rubric4_description = fields.Boolean(default=False)
    error_rubric5_name = fields.Boolean(default=False)
    error_rubric5_description = fields.Boolean(default=False)
    error_ophelia_rubric1_rating = fields.Boolean(default=False)
    error_ophelia_rubric2_rating = fields.Boolean(default=False)
    error_ophelia_rubric3_rating = fields.Boolean(default=False)
    error_ophelia_rubric4_rating = fields.Boolean(default=False)
    error_ophelia_rubric5_rating = fields.Boolean(default=False)
    error_opalite_rubric1_rating = fields.Boolean(default=False)
    error_opalite_rubric2_rating = fields.Boolean(default=False)
    error_opalite_rubric3_rating = fields.Boolean(default=False)
    error_opalite_rubric4_rating = fields.Boolean(default=False)
    error_opalite_rubric5_rating = fields.Boolean(default=False)
    error_gpt_rubric1_rating = fields.Boolean(default=False)
    error_gpt_rubric2_rating = fields.Boolean(default=False)
    error_gpt_rubric3_rating = fields.Boolean(default=False)
    error_gpt_rubric4_rating = fields.Boolean(default=False)
    error_gpt_rubric5_rating = fields.Boolean(default=False)
    error_gemini_rubric1_rating = fields.Boolean(default=False)
    error_gemini_rubric2_rating = fields.Boolean(default=False)
    error_gemini_rubric3_rating = fields.Boolean(default=False)
    error_gemini_rubric4_rating = fields.Boolean(default=False)
    error_gemini_rubric5_rating = fields.Boolean(default=False)
    error_gpt_preference = fields.Boolean(default=False)
    error_gpt_comment = fields.Boolean(default=False)
    error_gemini_preference = fields.Boolean(default=False)
    error_gemini_comment = fields.Boolean(default=False)

    gpt_response = fields.Text()
    gpt_reasoning = fields.Text(
        string="GPT Reasoning",
        help="Internal chain-of-thought / reasoning trace produced by the GPT model alongside its response. Backend use only.",
    )
    gemini_response = fields.Text()
    gemini_reasoning = fields.Text(
        string="Gemini Reasoning",
        help="Internal chain-of-thought / reasoning trace produced by the Gemini model alongside its response. Backend use only.",
    )

    qc_score = fields.Integer(string="QC score (/5)")
    is_tasker = fields.Boolean(compute="_compute_is_tasker")

    def evaluate_task(self):
        if self.truthfulness_a and self.store_truthfulness_a:
            self.error_truthfulness_a = check_error(
                int(self.truthfulness_a), int(self.store_truthfulness_a)
            )
        if self.instruction_following_a and self.store_instruction_following_a:
            self.error_instruction_following_a = check_error(
                int(self.instruction_following_a),
                int(self.store_instruction_following_a),
            )
        if self.writing_quality_a and self.store_writing_quality_a:
            self.error_writing_quality_a = check_error(
                int(self.writing_quality_a), int(self.store_writing_quality_a)
            )
        if self.verbosity_a and self.store_verbosity_a:
            self.error_verbosity_a = check_error(
                int(self.verbosity_a), int(self.store_verbosity_a)
            )
        if self.prompt_correctness_a and self.store_prompt_correctness_a:
            self.error_prompt_correctness_a = check_error(
                int(self.prompt_correctness_a), int(self.store_prompt_correctness_a)
            )
        if self.overall_quality_a and self.store_overall_quality_a:
            self.error_overall_quality_a = check_error(
                int(self.overall_quality_a), int(self.store_overall_quality_a)
            )
        if self.truthfulness_b and self.store_truthfulness_b:
            self.error_truthfulness_b = check_error(
                int(self.truthfulness_b), int(self.store_truthfulness_b)
            )
        if self.instruction_following_b and self.store_instruction_following_b:
            self.error_instruction_following_b = check_error(
                int(self.instruction_following_b),
                int(self.store_instruction_following_b),
            )
        if self.writing_quality_b and self.store_writing_quality_b:
            self.error_writing_quality_b = check_error(
                int(self.writing_quality_b), int(self.store_writing_quality_b)
            )
        if self.verbosity_b and self.store_verbosity_b:
            self.error_verbosity_b = check_error(
                int(self.verbosity_b), int(self.store_verbosity_b)
            )
        if self.prompt_correctness_b and self.store_prompt_correctness_b:
            self.error_prompt_correctness_b = check_error(
                int(self.prompt_correctness_b), int(self.store_prompt_correctness_b)
            )
        if self.overall_quality_b and self.store_overall_quality_b:
            self.error_overall_quality_b = check_error(
                int(self.overall_quality_b), int(self.store_overall_quality_b)
            )
        if self.ab_preference and self.store_ab_preference:
            self.error_ab_preference = check_error(
                int(self.ab_preference), int(self.store_ab_preference)
            )

        if self.ophelia_truthfulness_a and self.store_ophelia_truthfulness_a:
            self.error_ophelia_truthfulness_a = check_error(
                int(self.ophelia_truthfulness_a), int(self.store_ophelia_truthfulness_a)
            )
        if (
            self.ophelia_instruction_following_a
            and self.store_ophelia_instruction_following_a
        ):
            self.error_ophelia_instruction_following_a = check_error(
                int(self.ophelia_instruction_following_a),
                int(self.store_ophelia_instruction_following_a),
            )
        if self.ophelia_writing_quality_a and self.store_ophelia_writing_quality_a:
            self.error_ophelia_writing_quality_a = check_error(
                int(self.ophelia_writing_quality_a),
                int(self.store_ophelia_writing_quality_a),
            )
        if self.ophelia_verbosity_a and self.store_ophelia_verbosity_a:
            self.error_ophelia_verbosity_a = check_error(
                int(self.ophelia_verbosity_a), int(self.store_ophelia_verbosity_a)
            )
        if (
            self.ophelia_prompt_correctness_a
            and self.store_ophelia_prompt_correctness_a
        ):
            self.error_ophelia_prompt_correctness_a = check_error(
                int(self.ophelia_prompt_correctness_a),
                int(self.store_ophelia_prompt_correctness_a),
            )
        if self.ophelia_overall_quality_a and self.store_ophelia_overall_quality_a:
            self.error_ophelia_overall_quality_a = check_error(
                int(self.ophelia_overall_quality_a),
                int(self.store_ophelia_overall_quality_a),
            )
        if self.opalite_truthfulness_b and self.store_opalite_truthfulness_b:
            self.error_opalite_truthfulness_b = check_error(
                int(self.opalite_truthfulness_b), int(self.store_opalite_truthfulness_b)
            )
        if (
            self.opalite_instruction_following_b
            and self.store_opalite_instruction_following_b
        ):
            self.error_opalite_instruction_following_b = check_error(
                int(self.opalite_instruction_following_b),
                int(self.store_opalite_instruction_following_b),
            )
        if self.opalite_writing_quality_b and self.store_opalite_writing_quality_b:
            self.error_opalite_writing_quality_b = check_error(
                int(self.opalite_writing_quality_b),
                int(self.store_opalite_writing_quality_b),
            )
        if self.opalite_verbosity_b and self.store_opalite_verbosity_b:
            self.error_opalite_verbosity_b = check_error(
                int(self.opalite_verbosity_b), int(self.store_opalite_verbosity_b)
            )
        if (
            self.opalite_prompt_correctness_b
            and self.store_opalite_prompt_correctness_b
        ):
            self.error_opalite_prompt_correctness_b = check_error(
                int(self.opalite_prompt_correctness_b),
                int(self.store_opalite_prompt_correctness_b),
            )
        if self.opalite_overall_quality_b and self.store_opalite_overall_quality_b:
            self.error_opalite_overall_quality_b = check_error(
                int(self.opalite_overall_quality_b),
                int(self.store_opalite_overall_quality_b),
            )
        if self.enhance_ab_preference and self.store_enhance_ab_preference:
            self.error_enhance_ab_preference = check_error(
                int(self.enhance_ab_preference), int(self.store_enhance_ab_preference)
            )

        if self.gpt_truthfulness_a and self.store_gpt_truthfulness_a:
            self.error_gpt_truthfulness_a = check_error(
                int(self.gpt_truthfulness_a), int(self.store_gpt_truthfulness_a)
            )
        if self.gpt_instruction_following_a and self.store_gpt_instruction_following_a:
            self.error_gpt_instruction_following_a = check_error(
                int(self.gpt_instruction_following_a),
                int(self.store_gpt_instruction_following_a),
            )
        if self.gpt_writing_quality_a and self.store_gpt_writing_quality_a:
            self.error_gpt_writing_quality_a = check_error(
                int(self.gpt_writing_quality_a), int(self.store_gpt_writing_quality_a)
            )
        if self.gpt_verbosity_a and self.store_gpt_verbosity_a:
            self.error_gpt_verbosity_a = check_error(
                int(self.gpt_verbosity_a), int(self.store_gpt_verbosity_a)
            )
        if self.gpt_prompt_correctness_a and self.store_gpt_prompt_correctness_a:
            self.error_gpt_prompt_correctness_a = check_error(
                int(self.gpt_prompt_correctness_a),
                int(self.store_gpt_prompt_correctness_a),
            )
        if self.gpt_overall_quality_a and self.store_gpt_overall_quality_a:
            self.error_gpt_overall_quality_a = check_error(
                int(self.gpt_overall_quality_a), int(self.store_gpt_overall_quality_a)
            )
        if self.gemini_truthfulness_b and self.store_gemini_truthfulness_b:
            self.error_gemini_truthfulness_b = check_error(
                int(self.gemini_truthfulness_b), int(self.store_gemini_truthfulness_b)
            )
        if (
            self.gemini_instruction_following_b
            and self.store_gemini_instruction_following_b
        ):
            self.error_gemini_instruction_following_b = check_error(
                int(self.gemini_instruction_following_b),
                int(self.store_gemini_instruction_following_b),
            )
        if self.gemini_writing_quality_b and self.store_gemini_writing_quality_b:
            self.error_gemini_writing_quality_b = check_error(
                int(self.gemini_writing_quality_b),
                int(self.store_gemini_writing_quality_b),
            )
        if self.gemini_verbosity_b and self.store_gemini_verbosity_b:
            self.error_gemini_verbosity_b = check_error(
                int(self.gemini_verbosity_b), int(self.store_gemini_verbosity_b)
            )
        if self.gemini_prompt_correctness_b and self.store_gemini_prompt_correctness_b:
            self.error_gemini_prompt_correctness_b = check_error(
                int(self.gemini_prompt_correctness_b),
                int(self.store_gemini_prompt_correctness_b),
            )
        if self.gemini_overall_quality_b and self.store_gemini_overall_quality_b:
            self.error_gemini_overall_quality_b = check_error(
                int(self.gemini_overall_quality_b),
                int(self.store_gemini_overall_quality_b),
            )
        if self.gpts_ab_preference and self.store_gpts_ab_preference:
            self.error_gpts_ab_preference = check_error(
                int(self.gpts_ab_preference), int(self.store_gpts_ab_preference)
            )
        if self.geminis_ab_preference and self.store_geminis_ab_preference:
            self.error_geminis_ab_preference = check_error(
                int(self.geminis_ab_preference), int(self.store_geminis_ab_preference)
            )

        if self.ophelia_rubric1_rating and self.store_ophelia_rubric1_rating:
            self.error_ophelia_rubric1_rating = check_error(
                int(self.ophelia_rubric1_rating), int(self.store_ophelia_rubric1_rating)
            )
        if self.ophelia_rubric2_rating and self.store_ophelia_rubric2_rating:
            self.error_ophelia_rubric2_rating = check_error(
                int(self.ophelia_rubric2_rating), int(self.store_ophelia_rubric2_rating)
            )
        if self.ophelia_rubric3_rating and self.store_ophelia_rubric3_rating:
            self.error_ophelia_rubric3_rating = check_error(
                int(self.ophelia_rubric3_rating), int(self.store_ophelia_rubric3_rating)
            )
        if self.ophelia_rubric4_rating and self.store_ophelia_rubric4_rating:
            self.error_ophelia_rubric4_rating = check_error(
                int(self.ophelia_rubric4_rating), int(self.store_ophelia_rubric4_rating)
            )
        if self.ophelia_rubric5_rating and self.store_ophelia_rubric5_rating:
            self.error_ophelia_rubric5_rating = check_error(
                int(self.ophelia_rubric5_rating), int(self.store_ophelia_rubric5_rating)
            )
        if self.opalite_rubric1_rating and self.store_opalite_rubric1_rating:
            self.error_opalite_rubric1_rating = check_error(
                int(self.opalite_rubric1_rating), int(self.store_opalite_rubric1_rating)
            )
        if self.opalite_rubric2_rating and self.store_opalite_rubric2_rating:
            self.error_opalite_rubric2_rating = check_error(
                int(self.opalite_rubric2_rating), int(self.store_opalite_rubric2_rating)
            )
        if self.opalite_rubric3_rating and self.store_opalite_rubric3_rating:
            self.error_opalite_rubric3_rating = check_error(
                int(self.opalite_rubric3_rating), int(self.store_opalite_rubric3_rating)
            )
        if self.opalite_rubric4_rating and self.store_opalite_rubric4_rating:
            self.error_opalite_rubric4_rating = check_error(
                int(self.opalite_rubric4_rating), int(self.store_opalite_rubric4_rating)
            )
        if self.opalite_rubric5_rating and self.store_opalite_rubric5_rating:
            self.error_opalite_rubric5_rating = check_error(
                int(self.opalite_rubric5_rating), int(self.store_opalite_rubric5_rating)
            )
        if self.gpt_preference and self.store_gpt_preference:
            self.error_gpt_preference = check_error(
                int(self.gpt_preference), int(self.store_gpt_preference)
            )
        if self.gemini_preference and self.store_gemini_preference:
            self.error_gemini_preference = check_error(
                int(self.gemini_preference), int(self.store_gemini_preference)
            )
        if self.gpt_rubric1_rating and self.store_gpt_rubric1_rating:
            self.error_gpt_rubric1_rating = check_error(
                int(self.gpt_rubric1_rating), int(self.store_gpt_rubric1_rating)
            )
        if self.gpt_rubric2_rating and self.store_gpt_rubric2_rating:
            self.error_gpt_rubric2_rating = check_error(
                int(self.gpt_rubric2_rating), int(self.store_gpt_rubric2_rating)
            )
        if self.gpt_rubric3_rating and self.store_gpt_rubric3_rating:
            self.error_gpt_rubric3_rating = check_error(
                int(self.gpt_rubric3_rating), int(self.store_gpt_rubric3_rating)
            )
        if self.gpt_rubric4_rating and self.store_gpt_rubric4_rating:
            self.error_gpt_rubric4_rating = check_error(
                int(self.gpt_rubric4_rating), int(self.store_gpt_rubric4_rating)
            )
        if self.gpt_rubric5_rating and self.store_gpt_rubric5_rating:
            self.error_gpt_rubric5_rating = check_error(
                int(self.gpt_rubric5_rating), int(self.store_gpt_rubric5_rating)
            )
        if self.gemini_rubric1_rating and self.store_gemini_rubric1_rating:
            self.error_gemini_rubric1_rating = check_error(
                int(self.gemini_rubric1_rating), int(self.store_gemini_rubric1_rating)
            )
        if self.gemini_rubric2_rating and self.store_gemini_rubric2_rating:
            self.error_gemini_rubric2_rating = check_error(
                int(self.gemini_rubric2_rating), int(self.store_gemini_rubric2_rating)
            )
        if self.gemini_rubric3_rating and self.store_gemini_rubric3_rating:
            self.error_gemini_rubric3_rating = check_error(
                int(self.gemini_rubric3_rating), int(self.store_gemini_rubric3_rating)
            )
        if self.gemini_rubric4_rating and self.store_gemini_rubric4_rating:
            self.error_gemini_rubric4_rating = check_error(
                int(self.gemini_rubric4_rating), int(self.store_gemini_rubric4_rating)
            )
        if self.gemini_rubric5_rating and self.store_gemini_rubric5_rating:
            self.error_gemini_rubric5_rating = check_error(
                int(self.gemini_rubric5_rating), int(self.store_gemini_rubric5_rating)
            )
        if (
            self.ab_comment
            and self.store_ab_comment
            and self.ab_comment == self.store_ab_comment
        ):
            self.error_ab_comment = True
        if (
            self.enhance_ab_comment
            and self.store_enhance_ab_comment
            and self.enhance_ab_comment == self.store_enhance_ab_comment
        ):
            self.error_enhance_ab_comment = True
        if (
            self.geminis_ab_comment
            and self.store_geminis_ab_comment
            and self.geminis_ab_comment == self.store_geminis_ab_comment
        ):
            self.error_geminis_ab_comment = True
        if (
            self.gpts_ab_comment
            and self.store_gpts_ab_comment
            and self.gpts_ab_comment == self.store_gpts_ab_comment
        ):
            self.error_gpts_ab_comment = True
        if (
            self.rubric1_name
            and self.store_rubric1_name
            and self.rubric1_name == self.store_rubric1_name
        ):
            self.error_rubric1_name = True
        if (
            self.rubric1_description
            and self.store_rubric1_description
            and self.rubric1_description == self.store_rubric1_description
        ):
            self.error_rubric1_description = True
        if (
            self.rubric2_name
            and self.store_rubric2_name
            and self.rubric2_name == self.store_rubric2_name
        ):
            self.error_rubric2_name = True
        if (
            self.rubric2_description
            and self.store_rubric2_description
            and self.rubric2_description == self.store_rubric2_description
        ):
            self.error_rubric2_description = True
        if (
            self.rubric3_name
            and self.store_rubric3_name
            and self.rubric3_name == self.store_rubric3_name
        ):
            self.error_rubric3_name = True
        if (
            self.rubric3_description
            and self.store_rubric3_description
            and self.rubric3_description == self.store_rubric3_description
        ):
            self.error_rubric3_description = True
        if (
            self.rubric4_name
            and self.store_rubric4_name
            and self.rubric4_name == self.store_rubric4_name
        ):
            self.error_rubric4_name = True
        if (
            self.rubric4_description
            and self.store_rubric4_description
            and self.rubric4_description == self.store_rubric4_description
        ):
            self.error_rubric4_description = True
        if (
            self.rubric5_name
            and self.store_rubric5_name
            and self.rubric5_name == self.store_rubric5_name
        ):
            self.error_rubric5_name = True
        if (
            self.rubric5_description
            and self.store_rubric5_description
            and self.rubric5_description == self.store_rubric5_description
        ):
            self.error_rubric5_description = True
        if (
            self.gpt_comment
            and self.store_gpt_comment
            and self.gpt_comment == self.store_gpt_comment
        ):
            self.error_gpt_comment = True
        if (
            self.gemini_comment
            and self.store_gemini_comment
            and self.gemini_comment == self.store_gemini_comment
        ):
            self.error_gemini_comment = True
        self.is_eval_done = True

    def _safe_write(self, vals, label=""):
        """Write values with retry on PostgreSQL serialization failures.

        When multiple workers try to UPDATE the same record simultaneously,
        PostgreSQL throws 'could not serialize access due to concurrent update'.
        This method retries up to 3 times with exponential backoff.

        Uses PostgreSQL SAVEPOINTs so that a failure in one stage does NOT
        poison the entire Odoo request transaction.
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Use savepoint so that a failure here does NOT abort the
                # entire transaction — only this savepoint is rolled back.
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
                    wait = 0.5 * (2**attempt)  # 0.5s, 1s, 2s
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
                    # Refresh the recordset to get latest DB state
                    self.invalidate_recordset()
                    continue
                else:
                    _logger.error(
                        "Write failed for record %s (%s) after %d attempts: %s",
                        self.id,
                        label,
                        attempt + 1,
                        e,
                    )
                    raise
        return False

    def _stage_commit(self, label=""):
        """Flush dirty ORM fields to the database.

        This ensures Odoo writes all pending field changes to DB immediately,
        preventing them from accumulating into one giant UPDATE later.
        Does NOT call commit() — the Odoo framework manages transaction
        boundaries for XML-RPC calls.
        """
        try:
            self.env.flush_all()
            self.invalidate_recordset()
            _logger.debug("Stage flush OK for record %s (%s)", self.id, label)
        except Exception as e:
            _logger.warning(
                "Stage flush failed for record %s (%s): %s", self.id, label, e
            )

    def eval_task(self):
        import time as _time

        _eval_start = _time.time()
        kimi_api_key = os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY", "")

        task_id = self.task_id
        prompt = self.client_prompt
        resp_a = self.client_response_a
        resp_b = self.client_response_b

        # Stripped versions for eval / rubric / QC (no reasoning tags)
        resp_a_stripped = _strip_reasoning_tags(resp_a or "")
        resp_b_stripped = _strip_reasoning_tags(resp_b or "")

        rejection_reason = ""
        rejection_status = "ACCEPT"
        list1 = [prompt]
        tasks_response = llm_actions.prompt_rejection_check_sync_kimi(
            kimi_api_key=kimi_api_key, user_prompts=list1
        )
        if tasks_response:
            _logger.info("tasks1_response---------------------------%s", tasks_response)
            if (
                "status" in tasks_response[0]
                and tasks_response[0]["status"] != "ACCEPT"
            ):
                rejection_reason = tasks_response[0]["result"]["reason"]
                self.prompt_rejection_reason = rejection_reason
                self.is_processed = True
                self.is_ratable = False

            # Token usage tracking (Prompt Rejection)
            if (
                tasks_response
                and isinstance(tasks_response, list)
                and len(tasks_response) > 0
            ):
                usage = tasks_response[0].get("token_usage", {}).get("kimi", {})
                if usage.get("input_tokens") or usage.get("output_tokens"):
                    self.env["preference.ranking.token"].sudo().create(
                        {
                            "preference_record_ids": [(6, 0, [self.id])],
                            "type": "Prompt Rejection",
                            "ai_model_type": "kimi",
                            "token_line": [
                                (
                                    0,
                                    0,
                                    {
                                        "input_token": usage.get("input_tokens", 0),
                                        "output_token": usage.get("output_tokens", 0),
                                        "cache_token": usage.get("cached_tokens", 0),
                                        "cost": usage.get("cost_usd", 0.0),
                                    },
                                )
                            ],
                        }
                    )

        if rejection_status == "ACCEPT":
            # ── STEP 0: Generate missing prerequisite data ────────────────

            # Enhanced prompt
            enhance = self.enhance_prompt or ""
            if not enhance:
                _logger.info("Generating enhanced prompt for record %s", self.id)
                ep_result = llm_actions.enhance_prompt_sync_kimi(
                    kimi_api_key=kimi_api_key, input_prompt=prompt
                )
                if not ep_result.get("error"):
                    enhance = ep_result.get("enhanced_prompt", "")
                    self.enhance_prompt = enhance
                    # Token usage tracking (Prompt Enhancement)
                    usage = ep_result.get("token_usage", {}).get("kimi", {})
                    if usage.get("input_tokens") or usage.get("output_tokens"):
                        self.env["preference.ranking.token"].sudo().create(
                            {
                                "preference_record_ids": [(6, 0, [self.id])],
                                "type": "Prompt Enhancement",
                                "ai_model_type": "kimi",
                                "token_line": [
                                    (
                                        0,
                                        0,
                                        {
                                            "input_token": usage.get("input_tokens", 0),
                                            "output_token": usage.get(
                                                "output_tokens", 0
                                            ),
                                            "cache_token": usage.get(
                                                "cached_tokens", 0
                                            ),
                                            "cost": usage.get("cost_usd", 0.0),
                                        },
                                    )
                                ],
                            }
                        )
                else:
                    _logger.warning(
                        "Enhanced prompt generation failed: %s", ep_result["error"]
                    )

            # Use enhanced prompt for response generation (fall back to original)
            generation_prompt = enhance or prompt

            # GPT/Gemini and Ophelia/Opalite responses -- run in parallel
            gemini_resp = self.gemini_response or ""
            gpt_resp = self.gpt_response or ""
            ophelia_resp = self.ophelia_response_a or ""
            opalite_resp = self.opalite_response_b or ""

            # Treat prior error strings as empty so generation retries
            if gemini_resp.startswith("[error:"):
                _logger.info(
                    "Clearing stale Gemini error for record %s, will regenerate",
                    self.id,
                )
                gemini_resp = ""
                self.gemini_response = ""
            if gpt_resp.startswith("[error:"):
                _logger.info(
                    "Clearing stale GPT error for record %s, will regenerate", self.id
                )
                gpt_resp = ""
                self.gpt_response = ""

            need_gpt_gemini = not gemini_resp or not gpt_resp
            need_oo = not ophelia_resp or not opalite_resp

            def _gen_gpt_gemini():
                openai_api_key = llm_actions.get_openai_api_key()
                _logger.info("Generating GPT/Gemini responses for record %s", self.id)
                return llm_actions.response_generation_for_tasks_sync(
                    openai_api_key=openai_api_key or "",
                    tasks=[{"task_id": task_id, "prompt": generation_prompt}],
                )

            def _gen_ophelia_opalite():
                genai_api_key = llm_actions.get_genai_api_key()
                if not genai_api_key:
                    _logger.warning(
                        "Skipping Ophelia/Opalite generation: missing GENAI_ACCESS_TOKEN"
                    )
                    return None
                _logger.info(
                    "Generating Ophelia/Opalite responses for record %s", self.id
                )
                return llm_actions.generate_opalite_ophelia(
                    prompt=generation_prompt, genai_api_key=genai_api_key
                )

            if need_gpt_gemini or need_oo:
                with ThreadPoolExecutor(max_workers=2) as gen_pool:
                    gen_futures = {}
                    if need_gpt_gemini:
                        gen_futures["gpt_gemini"] = gen_pool.submit(_gen_gpt_gemini)
                    if need_oo:
                        gen_futures["oo"] = gen_pool.submit(_gen_ophelia_opalite)

                    response_vals = {}
                    if "gpt_gemini" in gen_futures:
                        gen_results = gen_futures["gpt_gemini"].result()
                        if gen_results:
                            if not gemini_resp:
                                gemini_resp = gen_results[0].get("gemini_response", "")
                                response_vals["gemini_response"] = gemini_resp
                            if not gpt_resp:
                                gpt_resp = gen_results[0].get("gpt_response", "")
                                response_vals["gpt_response"] = gpt_resp

                    if "oo" in gen_futures:
                        oo_result = gen_futures["oo"].result()
                        if oo_result:
                            oo_errors = oo_result.get("errors") or {}
                            if not ophelia_resp and "b" not in oo_errors:
                                ophelia_resp = oo_result.get("response_b", "")
                                response_vals["ophelia_response_a"] = ophelia_resp
                                response_vals["ophelia_reasoning"] = oo_result.get(
                                    "reasoning_b", ""
                                )
                            if not opalite_resp and "a" not in oo_errors:
                                opalite_resp = oo_result.get("response_a", "")
                                response_vals["opalite_response_b"] = opalite_resp

                            if oo_errors:
                                _logger.warning(
                                    "Ophelia/Opalite generation errors: %s",
                                    oo_errors,
                                )

                    # ── STAGE 1: Save LLM responses immediately (most critical data)
                    if response_vals:
                        self._safe_write(response_vals, label="responses")
                        self._stage_commit("responses")

            def _run_eval(label, eval_input):
                result = llm_actions.evaluation_for_tasks_sync_kimi(
                    kimi_api_key=kimi_api_key, evaluation_inputs=eval_input
                )
                _logger.info("%s response: %s", label, result)
                return label, result

            def _run_rubrics(rubric_input):
                result = llm_actions.batch_create_and_rate_rubrics_kimi(
                    kimi_api_key=kimi_api_key, tasks=rubric_input
                )
                _logger.info("rubric_results: %s", result)
                return "rubrics", result

            eval_input_ab = [
                {
                    "task_id": task_id,
                    "prompt": prompt,
                    "response_a": resp_a_stripped,
                    "response_b": resp_b_stripped,
                    "gemini_response": gemini_resp,
                    "gpt_response": gpt_resp,
                }
            ]
            ophelia_resp_stripped = _strip_reasoning_tags(ophelia_resp or "")
            opalite_resp_stripped = _strip_reasoning_tags(opalite_resp or "")
            eval_input_oph = [
                {
                    "task_id": task_id,
                    "prompt": prompt,
                    "response_a": ophelia_resp_stripped,
                    "response_b": opalite_resp_stripped,
                    "gemini_response": gemini_resp,
                    "gpt_response": gpt_resp,
                }
            ]
            eval_input_gpt = [
                {
                    "task_id": task_id,
                    "prompt": prompt,
                    "response_a": gpt_resp,
                    "response_b": resp_b_stripped,
                    "gemini_response": gemini_resp,
                    "gpt_response": gpt_resp,
                }
            ]
            eval_input_gem = [
                {
                    "task_id": task_id,
                    "prompt": prompt,
                    "response_a": resp_a_stripped,
                    "response_b": gemini_resp,
                    "gemini_response": gemini_resp,
                    "gpt_response": gpt_resp,
                }
            ]
            rubric_input = [
                {
                    "task_id": task_id,
                    "original_prompt": prompt,
                    "enhanced_prompt": enhance,
                    "opalite_response": opalite_resp_stripped or "",
                    "ophelia_response": ophelia_resp_stripped or "",
                    "gemini_response": gemini_resp or "",
                    "gpt_response": gpt_resp or "",
                }
            ]

            try:
                results = {}
                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = [
                        executor.submit(_run_eval, "eval_ab", eval_input_ab),
                        executor.submit(_run_eval, "eval_oph", eval_input_oph),
                        executor.submit(_run_eval, "eval_gpt_sxs", eval_input_gpt),
                        executor.submit(_run_eval, "eval_gem_sxs", eval_input_gem),
                        executor.submit(_run_rubrics, rubric_input),
                    ]
                    for future in as_completed(futures):
                        label, data = future.result()
                        results[label] = data

                dims = [
                    "truthfulness",
                    "instruction_following",
                    "writing_quality",
                    "verbosity",
                    "prompt_correctness",
                    "overall_quality",
                ]

                # Track stage failures — a failed stage should NOT block later stages
                _stage_errors = []

                # ── EVAL 1: Response A vs Response B ─────────────────────────
                tasks3_response = results.get("eval_ab")
                if tasks3_response:
                    r = tasks3_response[0]
                    scores_a = _extract_eval_scores(r, "response_a")
                    scores_b = _extract_eval_scores(r, "response_b")
                    comp_ab = _extract_comparison(r, "comparison_ab")
                    comp_gemini = _extract_comparison(r, "comparison_vs_gemini")
                    comp_gpt = _extract_comparison(r, "comparison_vs_gpt")
                    eval_ab_vals = {}
                    for d in dims:
                        eval_ab_vals[f"store_{d}_a"] = scores_a[d]["score"]
                        if not getattr(self, f"{d}_a"):
                            eval_ab_vals[f"{d}_a"] = scores_a[d]["score"]
                        eval_ab_vals[f"reason1_{d}_a"] = scores_a[d]["reason"]
                    for d in dims:
                        eval_ab_vals[f"store_{d}_b"] = scores_b[d]["score"]
                        if not getattr(self, f"{d}_b"):
                            eval_ab_vals[f"{d}_b"] = scores_b[d]["score"]
                        eval_ab_vals[f"reason1_{d}_b"] = scores_b[d]["reason"]
                    eval_ab_vals["store_ab_preference"] = comp_ab["score"]
                    if not self.ab_preference:
                        eval_ab_vals["ab_preference"] = comp_ab["score"]
                    eval_ab_vals["store_ab_comment"] = comp_ab["comment"]
                    if not self.ab_comment:
                        eval_ab_vals["ab_comment"] = comp_ab["comment"]
                    eval_ab_vals["store_gpt_preference"] = comp_gpt["score"]
                    eval_ab_vals["gpt_preference"] = comp_gpt["score"]
                    eval_ab_vals["store_gpt_comment"] = comp_gpt["comment"]
                    eval_ab_vals["gpt_comment"] = comp_gpt["comment"]
                    eval_ab_vals["store_gemini_preference"] = comp_gemini["score"]
                    eval_ab_vals["gemini_preference"] = comp_gemini["score"]
                    eval_ab_vals["store_gemini_comment"] = comp_gemini["comment"]
                    eval_ab_vals["gemini_comment"] = comp_gemini["comment"]
                    # ── STAGE 2: Save eval A vs B results
                    try:
                        self._safe_write(eval_ab_vals, label="eval_ab")
                        self._stage_commit("eval_ab")
                    except Exception as e:
                        _logger.error(
                            "Stage 2 (eval_ab) failed for record %s: %s", self.id, e
                        )
                        _stage_errors.append(("eval_ab", str(e)))

                # ── EVAL 2: Ophelia vs Opalite ───────────────────────────────
                oph_response = results.get("eval_oph")
                if oph_response:
                    r2 = oph_response[0]
                    scores_oph = _extract_eval_scores(r2, "response_a")
                    scores_opal = _extract_eval_scores(r2, "response_b")
                    comp_enhance = _extract_comparison(r2, "comparison_ab")
                    eval_oph_vals = {}
                    for d in dims:
                        eval_oph_vals[f"store_ophelia_{d}_a"] = scores_oph[d]["score"]
                        eval_oph_vals[f"ophelia_{d}_a"] = scores_oph[d]["score"]
                        eval_oph_vals[f"reason1_ophelia_{d}_a"] = scores_oph[d][
                            "reason"
                        ]
                    for d in dims:
                        eval_oph_vals[f"store_opalite_{d}_b"] = scores_opal[d]["score"]
                        eval_oph_vals[f"opalite_{d}_b"] = scores_opal[d]["score"]
                        eval_oph_vals[f"reason1_opalite_{d}_b"] = scores_opal[d][
                            "reason"
                        ]
                    eval_oph_vals["store_enhance_ab_preference"] = comp_enhance["score"]
                    eval_oph_vals["enhance_ab_preference"] = comp_enhance["score"]
                    eval_oph_vals["store_enhance_ab_comment"] = comp_enhance["comment"]
                    eval_oph_vals["enhance_ab_comment"] = comp_enhance["comment"]
                    # ── STAGE 3: Save Ophelia/Opalite eval results
                    try:
                        self._safe_write(eval_oph_vals, label="eval_oph")
                        self._stage_commit("eval_oph")
                    except Exception as e:
                        _logger.error(
                            "Stage 3 (eval_oph) failed for record %s: %s", self.id, e
                        )
                        _stage_errors.append(("eval_oph", str(e)))

                # ── EVAL 3: GPT SxS ─────────────────────────────────────────
                gpt_sxs_response = results.get("eval_gpt_sxs")
                if gpt_sxs_response:
                    r3 = gpt_sxs_response[0]
                    scores_gpt = _extract_eval_scores(r3, "response_a")
                    comp_gpts = _extract_comparison(r3, "comparison_ab")
                    eval_gpt_vals = {}
                    for d in dims:
                        eval_gpt_vals[f"store_gpt_{d}_a"] = scores_gpt[d]["score"]
                        eval_gpt_vals[f"gpt_{d}_a"] = scores_gpt[d]["score"]
                        eval_gpt_vals[f"reason1_gpt_{d}_a"] = scores_gpt[d]["reason"]
                    eval_gpt_vals["store_gpts_ab_preference"] = comp_gpts["score"]
                    eval_gpt_vals["gpts_ab_preference"] = comp_gpts["score"]
                    eval_gpt_vals["store_gpts_ab_comment"] = comp_gpts["comment"]
                    eval_gpt_vals["gpts_ab_comment"] = comp_gpts["comment"]
                    # ── STAGE 4: Save GPT SxS eval results
                    try:
                        self._safe_write(eval_gpt_vals, label="eval_gpt_sxs")
                        self._stage_commit("eval_gpt_sxs")
                    except Exception as e:
                        _logger.error(
                            "Stage 4 (eval_gpt_sxs) failed for record %s: %s",
                            self.id,
                            e,
                        )
                        _stage_errors.append(("eval_gpt_sxs", str(e)))

                # ── EVAL 4: Gemini SxS ──────────────────────────────────────
                gem_sxs_response = results.get("eval_gem_sxs")
                if gem_sxs_response:
                    r4 = gem_sxs_response[0]
                    scores_gem = _extract_eval_scores(r4, "response_b")
                    comp_gems = _extract_comparison(r4, "comparison_ab")
                    eval_gem_vals = {}
                    for d in dims:
                        eval_gem_vals[f"store_gemini_{d}_b"] = scores_gem[d]["score"]
                        eval_gem_vals[f"gemini_{d}_b"] = scores_gem[d]["score"]
                        eval_gem_vals[f"reason1_gemini_{d}_b"] = scores_gem[d]["reason"]
                    eval_gem_vals["store_geminis_ab_preference"] = comp_gems["score"]
                    eval_gem_vals["geminis_ab_preference"] = comp_gems["score"]
                    eval_gem_vals["store_geminis_ab_comment"] = comp_gems["comment"]
                    eval_gem_vals["geminis_ab_comment"] = comp_gems["comment"]
                    # ── STAGE 5: Save Gemini SxS eval results
                    try:
                        self._safe_write(eval_gem_vals, label="eval_gem_sxs")
                        self._stage_commit("eval_gem_sxs")
                    except Exception as e:
                        _logger.error(
                            "Stage 5 (eval_gem_sxs) failed for record %s: %s",
                            self.id,
                            e,
                        )
                        _stage_errors.append(("eval_gem_sxs", str(e)))

                # ── RUBRIC CREATION & RATING ─────────────────────────────────
                rubric_results = results.get("rubrics")
                if rubric_results:
                    rr = rubric_results[0]
                    rubrics = rr.get("rubrics", [])
                    ratings = rr.get("rubric_ratings", [])
                    rubric_vals = {}
                    for idx_r, rub in enumerate(rubrics[:5]):
                        rub_num = idx_r + 1
                        rubric_vals[f"store_rubric{rub_num}_name"] = rub.get("name", "")
                        rubric_vals[f"rubric{rub_num}_name"] = rub.get("name", "")
                        rubric_vals[f"store_rubric{rub_num}_description"] = rub.get(
                            "description", ""
                        )
                        rubric_vals[f"rubric{rub_num}_description"] = rub.get(
                            "description", ""
                        )
                    model_keys = ["ophelia", "opalite", "gpt", "gemini"]
                    for idx, rating in enumerate(ratings[:5]):
                        rub_num = idx + 1
                        for mk in model_keys:
                            raw_score = _safe_get(rating, mk, "score", default="")
                            score = _to_selection_score(raw_score, 1, 6)
                            reason = str(_safe_get(rating, mk, "reason", default=""))
                            rubric_vals[f"store_{mk}_rubric{rub_num}_rating"] = score
                            rubric_vals[f"{mk}_rubric{rub_num}_rating"] = score
                            rubric_vals[f"reason1_{mk}_rubric{rub_num}_rating"] = reason
                    # Write enhancement criteria categories
                    criteria = rr.get("criteria", [])
                    if criteria:
                        criteria_cmds = [(5, 0, 0)]  # Clear existing
                        for idx_c, crit_name in enumerate(criteria):
                            criteria_cmds.append(
                                (
                                    0,
                                    0,
                                    {
                                        "name": str(crit_name),
                                        "sequence": (idx_c + 1) * 10,
                                    },
                                )
                            )
                        rubric_vals["enhancement_criteria_ids"] = criteria_cmds
                    # ── STAGE 6: Save rubric results
                    try:
                        self._safe_write(rubric_vals, label="rubrics")
                        self._stage_commit("rubrics")
                    except Exception as e:
                        _logger.error(
                            "Stage 6 (rubrics) failed for record %s: %s", self.id, e
                        )
                        _stage_errors.append(("rubrics", str(e)))

                # ── TOKEN USAGE TRACKING (Evaluation) ──────────────────────────
                eval_labels = ["eval_ab", "eval_oph", "eval_gpt_sxs", "eval_gem_sxs"]
                total_kimi_tokens = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "cost_usd": 0.0,
                }

                # Evaluate aggregated Kimi tokens from eval tasks
                for label in eval_labels:
                    eval_res = results.get(label)
                    if eval_res and isinstance(eval_res, list) and len(eval_res) > 0:
                        usage = eval_res[0].get("token_usage", {}).get("kimi", {})
                        total_kimi_tokens["input_tokens"] += usage.get(
                            "input_tokens", 0
                        )
                        total_kimi_tokens["output_tokens"] += usage.get(
                            "output_tokens", 0
                        )
                        total_kimi_tokens["cached_tokens"] += usage.get(
                            "cached_tokens", 0
                        )
                        total_kimi_tokens["cost_usd"] += usage.get("cost_usd", 0.0)

                # Add tokens from rubrics
                # NOTE: batch_create_and_rate_rubrics_kimi returns usage under
                # the "usage" key (raw dict), NOT "token_usage.kimi".
                rubric_res = results.get("rubrics")
                if rubric_res and isinstance(rubric_res, list) and len(rubric_res) > 0:
                    usage = rubric_res[0].get("token_usage", {}).get("kimi", {})
                    total_kimi_tokens["input_tokens"] += usage.get("input_tokens", 0)
                    total_kimi_tokens["output_tokens"] += usage.get("output_tokens", 0)
                    total_kimi_tokens["cached_tokens"] += usage.get("cached_tokens", 0)
                    total_kimi_tokens["cost_usd"] += usage.get("cost_usd", 0.0)

                try:
                    if (
                        total_kimi_tokens["input_tokens"] > 0
                        or total_kimi_tokens["output_tokens"] > 0
                        or total_kimi_tokens["cost_usd"] > 0.0
                    ):
                        self.env["preference.ranking.token"].sudo().create(
                            {
                                "preference_record_ids": [(6, 0, [self.id])],
                                "type": "Eval",
                                "ai_model_type": "kimi",
                                "token_line": [
                                    (
                                        0,
                                        0,
                                        {
                                            "input_token": total_kimi_tokens[
                                                "input_tokens"
                                            ],
                                            "output_token": total_kimi_tokens[
                                                "output_tokens"
                                            ],
                                            "cache_token": total_kimi_tokens[
                                                "cached_tokens"
                                            ],
                                            "cost": total_kimi_tokens["cost_usd"],
                                        },
                                    )
                                ],
                            }
                        )
                except Exception as e:
                    _logger.error("Token tracking failed for record %s: %s", self.id, e)
                    _stage_errors.append(("token_tracking", str(e)))

                # Log summary of any stage failures
                if _stage_errors:
                    _logger.warning(
                        "Record %s had %d stage failure(s): %s",
                        self.id,
                        len(_stage_errors),
                        ", ".join(f"{s[0]}" for s in _stage_errors),
                    )

            except Exception as e:
                raise ValidationError(f"Error: {e}")

            # ── QC: run inline or publish to queue ─────────────────────────────
            inline_qc = os.getenv("INLINE_QC", "1").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            if inline_qc:
                _logger.info("Running QC inline for record %s", self.id)
                self.run_qc_checks()
            else:
                try:
                    publish_qc_task(self.id)
                    _logger.info("QC task published to RabbitMQ for record %s", self.id)
                except Exception as e:
                    _logger.warning(
                        "RabbitMQ publish failed for record %s, running QC inline: %s",
                        self.id,
                        e,
                    )
                    self.run_qc_checks()

            # ── STAGE 7: Mark record as processed
            self._safe_write(
                {
                    "is_processed": True,
                    "is_ratable": True,
                },
                label="finalize",
            )
            _eval_elapsed = _time.time() - _eval_start
            _logger.info(
                "eval_task completed for record %s in %.1fs", self.id, _eval_elapsed
            )
            return True

    def send_eval_to_rabbitmq(self):
        """Publish this record to the RabbitMQ eval queue for async processing."""
        publish_eval_task(self.id)

    def action_send_to_reeval(self):
        valid = self.filtered(lambda r: r.is_processed)
        if not valid:
            raise ValidationError(
                "No eligible records selected. Records must be processed."
            )
        batch_publish_reeval_tasks(valid.ids)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Re-evaluation Queued",
                "message": f"{len(valid.ids)} record(s) sent to re-evaluation queue.",
                "type": "success",
                "sticky": False,
            },
        }

    def reeval_task(self):
        import time as _time

        _eval_start = _time.time()
        kimi_api_key = os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY", "")

        dims = [
            "truthfulness",
            "instruction_following",
            "writing_quality",
            "verbosity",
            "prompt_correctness",
            "overall_quality",
        ]

        # ── STEP 0: Clear downstream fields (keep A/B scores, rubric names) ──
        clear_vals = {
            "is_eval_done": False,
            "qc_task_status": False,
            "task_status": "NotSubmitted",
            "gpt_response": False,
            "gemini_response": False,
            "ophelia_response_a": False,
            "opalite_response_b": False,
        }

        model_sections = [
            ("ophelia_", "_a"),
            ("opalite_", "_b"),
            ("gpt_", "_a"),
            ("gemini_", "_b"),
        ]
        for prefix, suffix in model_sections:
            for d in dims:
                field = f"{prefix}{d}{suffix}"
                clear_vals[field] = False
                clear_vals[f"store_{field}"] = False
                clear_vals[f"reason1_{field}"] = False
                clear_vals[f"error_{field}"] = False

        comparison_fields = [
            "enhance_ab_preference",
            "enhance_ab_comment",
            "gpts_ab_preference",
            "gpts_ab_comment",
            "geminis_ab_preference",
            "geminis_ab_comment",
            "gpt_preference",
            "gpt_comment",
            "gemini_preference",
            "gemini_comment",
        ]
        for field in comparison_fields:
            clear_vals[field] = False
            clear_vals[f"store_{field}"] = False
            clear_vals[f"reason1_{field}"] = False
            clear_vals[f"error_{field}"] = False

        for model in ["ophelia", "opalite", "gpt", "gemini"]:
            for n in [1, 2, 3, 4, 5]:
                field = f"{model}_rubric{n}_rating"
                clear_vals[field] = False
                clear_vals[f"store_{field}"] = False
                clear_vals[f"reason1_{field}"] = False
                clear_vals[f"error_{field}"] = False

        self.write(clear_vals)
        self._stage_commit("reeval_clear")

        # ── STEP 1: Read preserved data ──────────────────────────────────────
        task_id = self.task_id
        prompt = self.client_prompt
        resp_a = self.client_response_a
        resp_b = self.client_response_b
        enhance = self.enhance_prompt or ""
        generation_prompt = enhance or prompt

        # ── STEP 2: Regenerate all 4 model responses in parallel ─────────
        def _gen_gpt_gemini():
            openai_api_key = llm_actions.get_openai_api_key()
            _logger.info(
                "Reeval: Generating GPT/Gemini responses for record %s", self.id
            )
            return llm_actions.response_generation_for_tasks_sync(
                openai_api_key=openai_api_key or "",
                tasks=[{"task_id": task_id, "prompt": generation_prompt}],
            )

        def _gen_ophelia_opalite():
            genai_api_key = llm_actions.get_genai_api_key()
            if not genai_api_key:
                _logger.warning(
                    "Reeval: Skipping Ophelia/Opalite generation: missing GENAI_ACCESS_TOKEN"
                )
                return None
            _logger.info(
                "Reeval: Generating Ophelia/Opalite responses for record %s", self.id
            )
            return llm_actions.generate_opalite_ophelia(
                prompt=generation_prompt, genai_api_key=genai_api_key
            )

        gpt_resp = ""
        gemini_resp = ""
        ophelia_resp = ""
        opalite_resp = ""

        with ThreadPoolExecutor(max_workers=2) as gen_pool:
            gen_futures = {
                "gpt_gemini": gen_pool.submit(_gen_gpt_gemini),
                "oo": gen_pool.submit(_gen_ophelia_opalite),
            }

            response_vals = {}
            gen_results = gen_futures["gpt_gemini"].result()
            if gen_results:
                gemini_resp = gen_results[0].get("gemini_response", "")
                gpt_resp = gen_results[0].get("gpt_response", "")
                response_vals["gemini_response"] = gemini_resp
                response_vals["gpt_response"] = gpt_resp

            oo_result = gen_futures["oo"].result()
            if oo_result:
                oo_errors = oo_result.get("errors") or {}
                if "b" not in oo_errors:
                    ophelia_resp = oo_result.get("response_b", "")
                    response_vals["ophelia_response_a"] = ophelia_resp
                if "a" not in oo_errors:
                    opalite_resp = oo_result.get("response_a", "")
                    response_vals["opalite_response_b"] = opalite_resp
                if oo_errors:
                    _logger.warning(
                        "Reeval Ophelia/Opalite generation errors: %s", oo_errors
                    )

            if response_vals:
                self._safe_write(response_vals, label="reeval_responses")
                self._stage_commit("reeval_responses")

        # ── STEP 3: Build eval inputs ────────────────────────────────────────
        def _run_eval(label, eval_input):
            result = llm_actions.evaluation_for_tasks_sync_kimi(
                kimi_api_key=kimi_api_key, evaluation_inputs=eval_input
            )
            _logger.info("reeval %s response: %s", label, result)
            return label, result

        def _run_rubrics(rubric_input):
            result = llm_actions.batch_create_and_rate_rubrics_kimi(
                kimi_api_key=kimi_api_key, tasks=rubric_input
            )
            _logger.info("reeval rubric_results: %s", result)
            return "rubrics", result

        eval_input_oph = [
            {
                "task_id": task_id,
                "prompt": prompt,
                "response_a": ophelia_resp,
                "response_b": opalite_resp,
                "gemini_response": gemini_resp,
                "gpt_response": gpt_resp,
            }
        ]
        eval_input_gpt = [
            {
                "task_id": task_id,
                "prompt": prompt,
                "response_a": gpt_resp,
                "response_b": resp_b,
                "gemini_response": gemini_resp,
                "gpt_response": gpt_resp,
            }
        ]
        eval_input_gem = [
            {
                "task_id": task_id,
                "prompt": prompt,
                "response_a": resp_a,
                "response_b": gemini_resp,
                "gemini_response": gemini_resp,
                "gpt_response": gpt_resp,
            }
        ]
        rubric_input = [
            {
                "task_id": task_id,
                "original_prompt": prompt,
                "enhanced_prompt": enhance,
                "opalite_response": opalite_resp or "",
                "ophelia_response": ophelia_resp or "",
                "gemini_response": gemini_resp or "",
                "gpt_response": gpt_resp or "",
            }
        ]
        eval_input_ab = [
            {
                "task_id": task_id,
                "prompt": prompt,
                "response_a": _strip_reasoning_tags(resp_a or ""),
                "response_b": _strip_reasoning_tags(resp_b or ""),
                "gemini_response": gemini_resp,
                "gpt_response": gpt_resp,
            }
        ]

        # ── STEP 4: Run 5 parallel evaluations ──────────────────────────────
        try:
            results = {}
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [
                    executor.submit(_run_eval, "eval_ab", eval_input_ab),
                    executor.submit(_run_eval, "eval_oph", eval_input_oph),
                    executor.submit(_run_eval, "eval_gpt_sxs", eval_input_gpt),
                    executor.submit(_run_eval, "eval_gem_sxs", eval_input_gem),
                    executor.submit(_run_rubrics, rubric_input),
                ]
                for future in as_completed(futures):
                    label, data = future.result()
                    results[label] = data

            _stage_errors = []

            # ── A vs B (only GPT/Gemini preference & comment) ────────────
            eval_ab_response = results.get("eval_ab")
            if eval_ab_response:
                r_ab = eval_ab_response[0]
                comp_gpt = _extract_comparison(r_ab, "comparison_vs_gpt")
                comp_gemini = _extract_comparison(r_ab, "comparison_vs_gemini")
                eval_ab_vals = {
                    "store_gpt_preference": comp_gpt["score"],
                    "gpt_preference": comp_gpt["score"],
                    "store_gpt_comment": comp_gpt["comment"],
                    "gpt_comment": comp_gpt["comment"],
                    "store_gemini_preference": comp_gemini["score"],
                    "gemini_preference": comp_gemini["score"],
                    "store_gemini_comment": comp_gemini["comment"],
                    "gemini_comment": comp_gemini["comment"],
                }
                try:
                    self._safe_write(eval_ab_vals, label="reeval_ab")
                    self._stage_commit("reeval_ab")
                except Exception as e:
                    _logger.error("Reeval eval_ab failed for record %s: %s", self.id, e)
                    _stage_errors.append(("eval_ab", str(e)))

            # ── Ophelia vs Opalite ───────────────────────────────────────
            oph_response = results.get("eval_oph")
            if oph_response:
                r2 = oph_response[0]
                scores_oph = _extract_eval_scores(r2, "response_a")
                scores_opal = _extract_eval_scores(r2, "response_b")
                comp_enhance = _extract_comparison(r2, "comparison_ab")
                eval_oph_vals = {}
                for d in dims:
                    eval_oph_vals[f"store_ophelia_{d}_a"] = scores_oph[d]["score"]
                    eval_oph_vals[f"ophelia_{d}_a"] = scores_oph[d]["score"]
                    eval_oph_vals[f"reason1_ophelia_{d}_a"] = scores_oph[d]["reason"]
                for d in dims:
                    eval_oph_vals[f"store_opalite_{d}_b"] = scores_opal[d]["score"]
                    eval_oph_vals[f"opalite_{d}_b"] = scores_opal[d]["score"]
                    eval_oph_vals[f"reason1_opalite_{d}_b"] = scores_opal[d]["reason"]
                eval_oph_vals["store_enhance_ab_preference"] = comp_enhance["score"]
                eval_oph_vals["enhance_ab_preference"] = comp_enhance["score"]
                eval_oph_vals["store_enhance_ab_comment"] = comp_enhance["comment"]
                eval_oph_vals["enhance_ab_comment"] = comp_enhance["comment"]
                try:
                    self._safe_write(eval_oph_vals, label="reeval_oph")
                    self._stage_commit("reeval_oph")
                except Exception as e:
                    _logger.error(
                        "Reeval eval_oph failed for record %s: %s", self.id, e
                    )
                    _stage_errors.append(("eval_oph", str(e)))

            # ── GPT SxS ─────────────────────────────────────────────────
            gpt_sxs_response = results.get("eval_gpt_sxs")
            if gpt_sxs_response:
                r3 = gpt_sxs_response[0]
                scores_gpt = _extract_eval_scores(r3, "response_a")
                comp_gpts = _extract_comparison(r3, "comparison_ab")
                eval_gpt_vals = {}
                for d in dims:
                    eval_gpt_vals[f"store_gpt_{d}_a"] = scores_gpt[d]["score"]
                    eval_gpt_vals[f"gpt_{d}_a"] = scores_gpt[d]["score"]
                    eval_gpt_vals[f"reason1_gpt_{d}_a"] = scores_gpt[d]["reason"]
                eval_gpt_vals["store_gpts_ab_preference"] = comp_gpts["score"]
                eval_gpt_vals["gpts_ab_preference"] = comp_gpts["score"]
                eval_gpt_vals["store_gpts_ab_comment"] = comp_gpts["comment"]
                eval_gpt_vals["gpts_ab_comment"] = comp_gpts["comment"]
                try:
                    self._safe_write(eval_gpt_vals, label="reeval_gpt_sxs")
                    self._stage_commit("reeval_gpt_sxs")
                except Exception as e:
                    _logger.error(
                        "Reeval eval_gpt_sxs failed for record %s: %s", self.id, e
                    )
                    _stage_errors.append(("eval_gpt_sxs", str(e)))

            # ── Gemini SxS ───────────────────────────────────────────────
            gem_sxs_response = results.get("eval_gem_sxs")
            if gem_sxs_response:
                r4 = gem_sxs_response[0]
                scores_gem = _extract_eval_scores(r4, "response_b")
                comp_gems = _extract_comparison(r4, "comparison_ab")
                eval_gem_vals = {}
                for d in dims:
                    eval_gem_vals[f"store_gemini_{d}_b"] = scores_gem[d]["score"]
                    eval_gem_vals[f"gemini_{d}_b"] = scores_gem[d]["score"]
                    eval_gem_vals[f"reason1_gemini_{d}_b"] = scores_gem[d]["reason"]
                eval_gem_vals["store_geminis_ab_preference"] = comp_gems["score"]
                eval_gem_vals["geminis_ab_preference"] = comp_gems["score"]
                eval_gem_vals["store_geminis_ab_comment"] = comp_gems["comment"]
                eval_gem_vals["geminis_ab_comment"] = comp_gems["comment"]
                try:
                    self._safe_write(eval_gem_vals, label="reeval_gem_sxs")
                    self._stage_commit("reeval_gem_sxs")
                except Exception as e:
                    _logger.error(
                        "Reeval eval_gem_sxs failed for record %s: %s", self.id, e
                    )
                    _stage_errors.append(("eval_gem_sxs", str(e)))

            # ── Rubric ratings (names/descriptions untouched) ────────────
            rubric_results = results.get("rubrics")
            if rubric_results:
                rr = rubric_results[0]
                ratings = rr.get("rubric_ratings", [])
                rubric_vals = {}
                model_keys = ["ophelia", "opalite", "gpt", "gemini"]
                for idx, rating in enumerate(ratings[:5]):
                    rub_num = idx + 1
                    for mk in model_keys:
                        raw_score = _safe_get(rating, mk, "score", default="")
                        score = _to_selection_score(raw_score, 1, 6)
                        reason = str(_safe_get(rating, mk, "reason", default=""))
                        rubric_vals[f"store_{mk}_rubric{rub_num}_rating"] = score
                        rubric_vals[f"{mk}_rubric{rub_num}_rating"] = score
                        rubric_vals[f"reason1_{mk}_rubric{rub_num}_rating"] = reason
                try:
                    self._safe_write(rubric_vals, label="reeval_rubrics")
                    self._stage_commit("reeval_rubrics")
                except Exception as e:
                    _logger.error("Reeval rubrics failed for record %s: %s", self.id, e)
                    _stage_errors.append(("rubrics", str(e)))

            # ── Token usage tracking ─────────────────────────────────────
            eval_labels = ["eval_ab", "eval_oph", "eval_gpt_sxs", "eval_gem_sxs"]
            total_kimi_tokens = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "cost_usd": 0.0,
            }
            for label in eval_labels:
                eval_res = results.get(label)
                if eval_res and isinstance(eval_res, list) and len(eval_res) > 0:
                    usage = eval_res[0].get("token_usage", {}).get("kimi", {})
                    total_kimi_tokens["input_tokens"] += usage.get("input_tokens", 0)
                    total_kimi_tokens["output_tokens"] += usage.get("output_tokens", 0)
                    total_kimi_tokens["cached_tokens"] += usage.get("cached_tokens", 0)
                    total_kimi_tokens["cost_usd"] += usage.get("cost_usd", 0.0)

            rubric_res = results.get("rubrics")
            if rubric_res and isinstance(rubric_res, list) and len(rubric_res) > 0:
                usage = rubric_res[0].get("token_usage", {}).get("kimi", {})
                total_kimi_tokens["input_tokens"] += usage.get("input_tokens", 0)
                total_kimi_tokens["output_tokens"] += usage.get("output_tokens", 0)
                total_kimi_tokens["cached_tokens"] += usage.get("cached_tokens", 0)
                total_kimi_tokens["cost_usd"] += usage.get("cost_usd", 0.0)

            try:
                if (
                    total_kimi_tokens["input_tokens"] > 0
                    or total_kimi_tokens["output_tokens"] > 0
                    or total_kimi_tokens["cost_usd"] > 0.0
                ):
                    self.env["preference.ranking.token"].sudo().create(
                        {
                            "preference_record_ids": [(6, 0, [self.id])],
                            "type": "Reeval",
                            "ai_model_type": "kimi",
                            "token_line": [
                                (
                                    0,
                                    0,
                                    {
                                        "input_token": total_kimi_tokens[
                                            "input_tokens"
                                        ],
                                        "output_token": total_kimi_tokens[
                                            "output_tokens"
                                        ],
                                        "cache_token": total_kimi_tokens[
                                            "cached_tokens"
                                        ],
                                        "cost": total_kimi_tokens["cost_usd"],
                                    },
                                )
                            ],
                        }
                    )
            except Exception as e:
                _logger.error(
                    "Reeval token tracking failed for record %s: %s", self.id, e
                )
                _stage_errors.append(("token_tracking", str(e)))

            if _stage_errors:
                _logger.warning(
                    "Reeval record %s had %d stage failure(s): %s",
                    self.id,
                    len(_stage_errors),
                    ", ".join(f"{s[0]}" for s in _stage_errors),
                )

        except Exception as e:
            raise ValidationError(f"Re-evaluation error: {e}")

        # ── QC: run inline or publish to queue ───────────────────────────
        inline_qc = os.getenv("INLINE_QC", "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if inline_qc:
            _logger.info("Running QC inline for reeval record %s", self.id)
            self.run_qc_checks()
        else:
            try:
                publish_qc_task(self.id)
                _logger.info(
                    "QC task published to RabbitMQ for reeval record %s", self.id
                )
            except Exception as e:
                _logger.warning(
                    "RabbitMQ publish failed for reeval record %s, running QC inline: %s",
                    self.id,
                    e,
                )
                self.run_qc_checks()

        self._safe_write(
            {
                "is_processed": True,
                "is_ratable": True,
            },
            label="reeval_finalize",
        )
        _eval_elapsed = _time.time() - _eval_start
        _logger.info(
            "reeval_task completed for record %s in %.1fs", self.id, _eval_elapsed
        )
        return True

    def _run_error_checking(self):
        """Compare human ratings vs LLM store values across all sections."""
        dims = [
            "truthfulness",
            "instruction_following",
            "writing_quality",
            "verbosity",
            "prompt_correctness",
            "overall_quality",
        ]
        sections = [
            ("", "_a"),  # Response A
            ("", "_b"),  # Response B
            ("ophelia_", "_a"),  # Ophelia
            ("opalite_", "_b"),  # Opalite
            ("gpt_", "_a"),  # GPT SxS
            ("gemini_", "_b"),  # Gemini SxS
        ]
        for prefix, suffix in sections:
            for d in dims:
                human_field = f"{prefix}{d}{suffix}"
                store_field = f"store_{prefix}{d}{suffix}"
                error_field = f"error_{prefix}{d}{suffix}"
                human_val = getattr(self, human_field, None)
                store_val = getattr(self, store_field, None)
                if human_val and store_val:
                    setattr(
                        self, error_field, check_error(int(human_val), int(store_val))
                    )

    def run_qc_checks(self):
        """Run QC checks on this record. Called by consumer or inline fallback."""
        import time as _time

        _qc_start = _time.time()
        kimi_api_key = os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY", "")
        dims = [
            "truthfulness",
            "instruction_following",
            "writing_quality",
            "verbosity",
            "prompt_correctness",
            "overall_quality",
        ]
        # ── QC CHECKS ─────────────────────────────────────────────────────
        qc_inputs = [
            {
                "ab_comment": self.ab_comment,
                "ab_preference": self.ab_preference,
                "human_ab_gpt_comment": self.gpt_comment,
                "human_ab_gemini_comment": self.gemini_comment,
                "ab_gpt_preference": self.gpt_preference,
                "ab_gemini_preference": self.gemini_preference,
                "human_gpt_rubric_name": self.rubric1_name,
                "human_gpt_rubric_description": self.rubric1_description,
                "human_gpt_rubric_scale_rating": self.gpt_rubric1_rating,
                "human_gemini_rubric_name": self.rubric2_name,
                "human_gemini_rubric_description": self.rubric2_description,
                "human_gemini_rubric_scale_rating": self.gemini_rubric2_rating,
                "response_a": _strip_reasoning_tags(self.client_response_a or ""),
                "response_b": _strip_reasoning_tags(self.client_response_b or ""),
                "gemini_response": self.gemini_response,
                "gpt_response": self.gpt_response,
            }
        ]
        data = llm_actions.perform_qc_checks_sync_kimi(
            kimi_api_key=kimi_api_key, qc_inputs=qc_inputs
        )
        _logger.info("qc_data: %s", data)

        if data:
            d0 = data[0]
            checks = d0.get("checks", {})
            qc_vals = {}
            qc_vals["qc_task_status"] = (
                "pass" if d0.get("qc_status") == "QC_Pass" else "fail"
            )

            # CHECK 3: AB preference vs comment grounding
            pref_ground = checks.get("ab_preference_comment_grounding", {})
            pmc = pref_ground.get("preference_matches_comment", {})
            if not pmc.get("result", True):
                qc_vals["reason1_ab_preference"] = pmc.get("issue", "")

            # CHECK 1: AI detection flagged fields
            ai_det = checks.get("ai_detection", {})
            flagged = ai_det.get("flagged_fields", {})

            qc_vals["reason1_ab_comment"] = (
                flagged.get("ab_comment", [""])[0] if flagged.get("ab_comment") else ""
            )
            qc_vals["reason1_gpt_comment"] = (
                flagged.get("human_ab_gpt_comment", [""])[0]
                if flagged.get("human_ab_gpt_comment")
                else ""
            )
            qc_vals["reason1_gemini_comment"] = (
                flagged.get("human_ab_gemini_comment", [""])[0]
                if flagged.get("human_ab_gemini_comment")
                else ""
            )
            qc_vals["reason1_rubric1_name"] = (
                flagged.get("human_gpt_rubric_name", [""])[0]
                if flagged.get("human_gpt_rubric_name")
                else ""
            )
            qc_vals["reason1_rubric1_description"] = (
                flagged.get("human_gpt_rubric_description", [""])[0]
                if flagged.get("human_gpt_rubric_description")
                else ""
            )
            qc_vals["reason1_rubric2_name"] = (
                flagged.get("human_gemini_rubric_name", [""])[0]
                if flagged.get("human_gemini_rubric_name")
                else ""
            )
            qc_vals["reason1_rubric2_description"] = (
                flagged.get("human_gemini_rubric_description", [""])[0]
                if flagged.get("human_gemini_rubric_description")
                else ""
            )

            # CHECK 2: Rubric-comment grounding
            rcg = checks.get("rubric_comment_grounding", {})

            # GPT grounding
            gpt_g = rcg.get("gpt_grounding", {})
            ng = gpt_g.get("name_grounded", {})
            if not ng.get("result", True) and ng.get("issue"):
                qc_vals["reason1_rubric1_name"] = (
                    (qc_vals.get("reason1_rubric1_name") or "") + "\n" + ng["issue"]
                )
            dg = gpt_g.get("description_grounded", {})
            if not dg.get("result", True) and dg.get("issue"):
                qc_vals["reason1_rubric1_description"] = (
                    (qc_vals.get("reason1_rubric1_description") or "")
                    + "\n"
                    + dg["issue"]
                )
            rc_gpt = gpt_g.get("rating_consistent", {})
            if not rc_gpt.get("result", True) and rc_gpt.get("issue"):
                qc_vals["reason1_gpt_preference"] = rc_gpt["issue"]

            # Gemini grounding
            gem_g = rcg.get("gemini_grounding", {})
            ng2 = gem_g.get("name_grounded", {})
            if not ng2.get("result", True) and ng2.get("issue"):
                qc_vals["reason1_rubric2_name"] = (
                    (qc_vals.get("reason1_rubric2_name") or "") + "\n" + ng2["issue"]
                )
            dg2 = gem_g.get("description_grounded", {})
            if not dg2.get("result", True) and dg2.get("issue"):
                qc_vals["reason1_rubric2_description"] = (
                    (qc_vals.get("reason1_rubric2_description") or "")
                    + "\n"
                    + dg2["issue"]
                )
            rc_gem = gem_g.get("rating_consistent", {})
            if not rc_gem.get("result", True) and rc_gem.get("issue"):
                qc_vals["reason1_gemini_preference"] = rc_gem["issue"]

            # Comment grounded in responses
            cgr = rcg.get("comment_grounded_in_responses", {})
            gpt_cgr = cgr.get("gpt_comment_grounded_in_responses", {})
            if not gpt_cgr.get("result", True) and gpt_cgr.get("issue"):
                qc_vals["reason1_gpt_comment"] = (
                    (qc_vals.get("reason1_gpt_comment") or "") + "\n" + gpt_cgr["issue"]
                )
            gem_cgr = cgr.get("gemini_comment_grounded_in_responses", {})
            if not gem_cgr.get("result", True) and gem_cgr.get("issue"):
                qc_vals["reason1_gemini_comment"] = (
                    (qc_vals.get("reason1_gemini_comment") or "")
                    + "\n"
                    + gem_cgr["issue"]
                )

            # AB comment grounded in responses
            ab_cgr = pref_ground.get("ab_comment_grounded_in_responses", {})
            if not ab_cgr.get("result", True) and ab_cgr.get("issue"):
                qc_vals["reason1_ab_comment"] = (
                    (qc_vals.get("reason1_ab_comment") or "") + "\n" + ab_cgr["issue"]
                )

            # CHECK 4: Rubric rating justification
            rrj = checks.get("rubric_rating_justification", {})
            gpt_rj = rrj.get("gpt_rating_justified", {})
            if not gpt_rj.get("result", True) and gpt_rj.get("issue"):
                qc_vals["reason1_gpt_rubric1_rating"] = gpt_rj["issue"]
            gem_rj = rrj.get("gemini_rating_justified", {})
            if not gem_rj.get("result", True) and gem_rj.get("issue"):
                qc_vals["reason1_gemini_rubric2_rating"] = gem_rj["issue"]

            # CHECK 5: External preference vs comment grounding
            epcg = checks.get("external_preference_comment_grounding", {})
            gpt_pmc = epcg.get("gpt_preference_matches_comment", {})
            if not gpt_pmc.get("result", True) and gpt_pmc.get("issue"):
                qc_vals["reason1_gpt_preference"] = (
                    (qc_vals.get("reason1_gpt_preference") or "")
                    + "\n"
                    + gpt_pmc["issue"]
                )
            gem_pmc = epcg.get("gemini_preference_matches_comment", {})
            if not gem_pmc.get("result", True) and gem_pmc.get("issue"):
                qc_vals["reason1_gemini_preference"] = (
                    (qc_vals.get("reason1_gemini_preference") or "")
                    + "\n"
                    + gem_pmc["issue"]
                )

            # Write all QC results with retry
            self._safe_write(qc_vals, label="qc_checks")
            self._stage_commit("qc_checks")

            # Token usage tracking
            token_usage = d0.get("token_usage", {})
            oai = token_usage.get("openai", {})
            gem = token_usage.get("gemini", {})
            kimi = token_usage.get("kimi", {})

            gpt_in = oai.get("input_tokens", 0) or 0
            gpt_out = oai.get("output_tokens", 0) or 0
            gpt_cache = oai.get("cached_tokens", 0) or 0
            gpt_cost = oai.get("cost_usd", 0.0) or 0.0
            gem_in = gem.get("input_tokens", 0) or 0
            gem_out = gem.get("output_tokens", 0) or 0
            gem_cache = gem.get("cached_tokens", 0) or 0
            gem_cost = gem.get("cost_usd", 0.0) or 0.0

            kimi_in = kimi.get("input_tokens", 0) or 0
            kimi_out = kimi.get("output_tokens", 0) or 0
            kimi_cache = kimi.get("cached_tokens", 0) or 0
            kimi_cost = kimi.get("cost_usd", 0.0) or 0.0

            if gpt_in > 0 or gpt_out > 0 or gpt_cache > 0 or gpt_cost > 0.0:
                self.env["preference.ranking.token"].sudo().create(
                    {
                        "preference_record_ids": [(6, 0, [self.id])],
                        "type": "QC",
                        "ai_model_type": "openai",
                        "token_line": [
                            (
                                0,
                                0,
                                {
                                    "input_token": gpt_in,
                                    "output_token": gpt_out,
                                    "cache_token": gpt_cache,
                                    "cost": gpt_cost,
                                },
                            )
                        ],
                    }
                )
            if gem_in > 0 or gem_out > 0 or gem_cache > 0 or gem_cost > 0.0:
                self.env["preference.ranking.token"].sudo().create(
                    {
                        "preference_record_ids": [(6, 0, [self.id])],
                        "type": "QC",
                        "ai_model_type": "gemini",
                        "token_line": [
                            (
                                0,
                                0,
                                {
                                    "input_token": gem_in,
                                    "output_token": gem_out,
                                    "cache_token": gem_cache,
                                    "cost": gem_cost,
                                },
                            )
                        ],
                    }
                )
            if kimi_in > 0 or kimi_out > 0 or kimi_cache > 0 or kimi_cost > 0.0:
                self.env["preference.ranking.token"].sudo().create(
                    {
                        "preference_record_ids": [(6, 0, [self.id])],
                        "type": "QC",
                        "ai_model_type": "kimi",
                        "token_line": [
                            (
                                0,
                                0,
                                {
                                    "input_token": kimi_in,
                                    "output_token": kimi_out,
                                    "cache_token": kimi_cache,
                                    "cost": kimi_cost,
                                },
                            )
                        ],
                    }
                )

        _qc_elapsed = _time.time() - _qc_start
        _logger.info(
            "run_qc_checks completed for record %s in %.1fs", self.id, _qc_elapsed
        )
        return True

    def action_submit_prompt(self):
        self.error_ophelia_truthfulness_a = False
        self.error_ophelia_instruction_following_a = False
        self.error_ophelia_writing_quality_a = False
        self.error_ophelia_verbosity_a = False
        self.error_ophelia_prompt_correctness_a = False
        self.error_ophelia_overall_quality_a = False
        self.error_opalite_truthfulness_b = False
        self.error_opalite_instruction_following_b = False
        self.error_opalite_writing_quality_b = False
        self.error_opalite_verbosity_b = False
        self.error_opalite_prompt_correctness_b = False
        self.error_opalite_overall_quality_b = False
        self.error_enhance_ab_preference = False
        self.error_gpt_truthfulness_a = False
        self.error_gpt_instruction_following_a = False
        self.error_gpt_writing_quality_a = False
        self.error_gpt_verbosity_a = False
        self.error_gpt_prompt_correctness_a = False
        self.error_gpt_overall_quality_a = False
        self.error_gemini_truthfulness_b = False
        self.error_gemini_instruction_following_b = False
        self.error_gemini_writing_quality_b = False
        self.error_gemini_verbosity_b = False
        self.error_gemini_prompt_correctness_b = False
        self.error_gemini_overall_quality_b = False
        self.error_gpts_ab_preference = False
        self.error_geminis_ab_preference = False
        self.error_ophelia_rubric1_rating = False
        self.error_ophelia_rubric2_rating = False
        self.error_ophelia_rubric3_rating = False
        self.error_ophelia_rubric4_rating = False
        self.error_ophelia_rubric5_rating = False
        self.error_opalite_rubric1_rating = False
        self.error_opalite_rubric2_rating = False
        self.error_opalite_rubric3_rating = False
        self.error_opalite_rubric4_rating = False
        self.error_opalite_rubric5_rating = False
        self.error_gpt_preference = False
        self.error_gemini_preference = False
        self.error_gpt_rubric1_rating = False
        self.error_gpt_rubric2_rating = False
        self.error_gpt_rubric3_rating = False
        self.error_gpt_rubric4_rating = False
        self.error_gpt_rubric5_rating = False
        self.error_gemini_rubric1_rating = False
        self.error_gemini_rubric2_rating = False
        self.error_gemini_rubric3_rating = False
        self.error_gemini_rubric4_rating = False
        self.error_gemini_rubric5_rating = False
        self.is_eval_done = False

        """Clear all fields downstream of enhance_prompt and trigger re-evaluation."""
        dims = [
            "truthfulness",
            "instruction_following",
            "writing_quality",
            "verbosity",
            "prompt_correctness",
            "overall_quality",
        ]

        clear_vals = {
            "ophelia_response_a": False,
            "opalite_response_b": False,
            "gpt_response": False,
            "gemini_response": False,
            "is_eval_done": False,
            "is_processed": False,
            "qc_task_status": False,
        }

        model_sections = [
            ("ophelia_", "_a"),
            ("opalite_", "_b"),
            ("gpt_", "_a"),
            ("gemini_", "_b"),
        ]
        for prefix, suffix in model_sections:
            for d in dims:
                field = f"{prefix}{d}{suffix}"
                clear_vals[field] = False
                clear_vals[f"store_{field}"] = False
                clear_vals[f"reason1_{field}"] = False
                clear_vals[f"error_{field}"] = False

        comparison_fields = [
            "enhance_ab_preference",
            "enhance_ab_comment",
            "gpts_ab_preference",
            "gpts_ab_comment",
            "geminis_ab_preference",
            "geminis_ab_comment",
            "gpt_preference",
            "gpt_comment",
            "gemini_preference",
            "gemini_comment",
        ]
        for field in comparison_fields:
            clear_vals[field] = False
            clear_vals[f"store_{field}"] = False
            clear_vals[f"reason1_{field}"] = False
            clear_vals[f"error_{field}"] = False

        for n in [1, 2, 3, 4, 5]:
            for base in ["name", "description"]:
                field = f"rubric{n}_{base}"
                clear_vals[field] = False
                clear_vals[f"store_{field}"] = False
                clear_vals[f"reason1_{field}"] = False
                clear_vals[f"error_{field}"] = False

        for model in ["ophelia", "opalite", "gpt", "gemini"]:
            for n in [1, 2, 3, 4, 5]:
                field = f"{model}_rubric{n}_rating"
                clear_vals[field] = False
                clear_vals[f"store_{field}"] = False
                clear_vals[f"reason1_{field}"] = False
                clear_vals[f"error_{field}"] = False

        self.write(clear_vals)
        _logger.info(
            "Enhanced prompt changed for record %s — cleared downstream fields, running eval inline",
            self.id,
        )
        self.eval_task()

        return True

    def submit_task(self):
        if not self.is_eval_done:
            raise ValidationError(f"Evaluation Not Done!")
        self.task_status = "Submitted"
        return True
