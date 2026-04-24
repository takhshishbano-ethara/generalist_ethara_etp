# -*- coding: utf-8 -*-
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import httpx

from odoo import api, http, SUPERUSER_ID
from odoo.http import request
from odoo.modules.module import get_module_path
from odoo.modules.registry import Registry

from ..models.atlas import _load_dotenv, _get_prompt

_logger = logging.getLogger(__name__)

BEDROCK_CONVERSE_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
)

_system_prompt_cache = {"text": None, "mtime": 0}

_RUBRIC_QC_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="rubric_qc")


def _get_system_prompt():
    mod_path = get_module_path("atlas")
    if not mod_path:
        return ""

    for filename in ("prompts/qc_prompt.md", "system_prompts.md"):
        path = os.path.join(mod_path, filename)
        if not os.path.isfile(path):
            continue

        mtime = os.path.getmtime(path)
        if _system_prompt_cache["text"] is not None and _system_prompt_cache["mtime"] == mtime:
            return _system_prompt_cache["text"]

        with open(path, "r") as f:
            _system_prompt_cache["text"] = f.read().strip()
        _system_prompt_cache["mtime"] = mtime
        return _system_prompt_cache["text"]

    return ""


def _parse_json_response(text):
    cleaned = text.strip()
    json_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
    if json_block:
        cleaned = json_block.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _parse_qc_verdict(text):
    parsed = _parse_json_response(text)
    if parsed and isinstance(parsed, dict) and parsed.get("severity"):
        severity = (parsed.get("severity") or "").strip().lower()
        valid_severities = ("low", "medium", "high", "critical")
        if severity not in valid_severities:
            severity = "medium"
        return {
            "severity": severity,
            "summary": parsed.get("summary", ""),
            "total_fails": int(parsed.get("total_fails", 0)),
            "total_warns": int(parsed.get("total_warns", 0)),
            "total_passes": int(parsed.get("total_passes", 0)),
            "checks": parsed.get("checks", []),
        }

    verdict_match = re.search(
        r"Overall\s+Verdict:\s*(PASS|FAIL)", text, re.IGNORECASE
    )
    check_rows = re.findall(
        r"\|\s*\d+\s*\|[^|]+\|\s*(PASS|FAIL)\s*\|([^|]*)\|", text, re.IGNORECASE
    )

    if not verdict_match and not check_rows:
        return None

    overall = (verdict_match.group(1).strip().upper() if verdict_match else "").upper()
    fails = sum(1 for r, _ in check_rows if r.strip().upper() == "FAIL")
    passes = sum(1 for r, _ in check_rows if r.strip().upper() == "PASS")

    if overall == "PASS" and fails == 0:
        severity = "low"
    elif fails == 1:
        severity = "medium"
    elif fails == 2:
        severity = "high"
    elif fails >= 3:
        severity = "critical"
    else:
        severity = "low" if overall == "PASS" else "high"

    checks = []
    check_names = ["Grammar & Language", "Clear Ask", "Realistic", "Feasible"]
    for i, (result, finding) in enumerate(check_rows):
        checks.append({
            "check": i + 1,
            "name": check_names[i] if i < len(check_names) else "Check %d" % (i + 1),
            "verdict": result.strip().upper(),
            "reason": finding.strip() if result.strip().upper() == "FAIL" else "",
        })

    rewrite_match = re.search(
        r"### Suggested Rewrite.*?\n>\s*(.+?)(?:\n\n|\n###|\Z)", text, re.DOTALL
    )
    summary_parts = []
    if fails > 0:
        failed_names = [c["name"] for c in checks if c["verdict"] == "FAIL"]
        summary_parts.append("Failed: %s" % ", ".join(failed_names))
    if rewrite_match:
        summary_parts.append("Rewrite suggested")

    return {
        "severity": severity,
        "summary": "; ".join(summary_parts) if summary_parts else ("All checks passed" if overall == "PASS" else ""),
        "total_fails": fails,
        "total_warns": 0,
        "total_passes": passes,
        "checks": checks,
    }


def _is_degenerate(text, threshold=0.8):
    if not text or len(text) < 20:
        return False
    from collections import Counter

    counts = Counter(text)
    most_common_count = counts.most_common(1)[0][1]
    return most_common_count / len(text) >= threshold


def _run_rubric_criterion_qc_background(db_name, criterion_id, task_id, session_id, notify_partner_id):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            criterion = env["atlas.rubric.criterion"].browse(criterion_id)
            if not criterion.exists():
                return

            task = env["atlas.atlas"].browse(task_id)
            if not task.exists():
                criterion.write({"qc_status": "error", "qc_feedback": "Task not found"})
                return

            ICP = env["ir.config_parameter"].sudo()
            inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("atlas.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

            if not api_key or not inference_arn:
                criterion.write({"qc_status": "error", "qc_feedback": "Missing Bedrock credentials"})
                return

            system_prompt = _get_prompt("rubric_qc_prompt.md")
            if not system_prompt:
                criterion.write({"qc_status": "error", "qc_feedback": "Missing rubric_qc_prompt.md"})
                return

            if session_id:
                turns = task.turn_ids.filtered(
                    lambda t, sid=session_id: t.session_id == sid
                ).sorted("turn_number")
            else:
                turns = task.turn_ids.sorted("turn_number")
            conversation_parts = []
            for t in turns:
                if t.prompt and not t.is_hint_turn:
                    conversation_parts.append("User: %s" % t.prompt.strip()[:800])
                if t.response:
                    conversation_parts.append("Assistant: %s" % t.response.strip()[:800])

            if not conversation_parts:
                criterion.write({"qc_status": "error", "qc_feedback": "No conversation data"})
                return

            all_criteria = task.rubric_criterion_ids.sorted("sequence, id")
            rubric_rows = []
            for i, c in enumerate(all_criteria, 1):
                prefix = "N%d" % (i - len([x for x in all_criteria if not x.is_negative])) if c.is_negative else str(i)
                sign = "❌" if c.is_negative else "✅"
                w = "-%d" % c.weight if c.is_negative else "+%d" % c.weight
                rubric_rows.append("| %s | %s | %s | %s | %s |" % (
                    prefix, c.name, w, sign, c.suggestion or "",
                ))

            goal = task.goal_description or "(No goal generated)"
            rubric_table = (
                "| # | Criterion | Weight | +/- | Grounding |\n"
                "|---|-----------|--------|-----|-----------|"
            )
            if rubric_rows:
                rubric_table += "\n" + "\n".join(rubric_rows)

            user_message = "## Goal\n%s\n\n## Rubric\n%s\n\n## Conversation\n%s" % (
                goal,
                rubric_table,
                "\n".join(conversation_parts),
            )

            try:
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

                if _is_degenerate(response_text):
                    response_text, usage = _call_bedrock_converse(
                        api_key=api_key,
                        inference_arn=inference_arn,
                        region=region,
                        system_prompt=system_prompt,
                        user_message=user_message,
                        max_tokens=1024,
                        temperature=0.1,
                        top_p=0.7,
                        timeout=90.0,
                    )
            except Exception:
                _logger.exception("Rubric QC bg: Bedrock call failed for criterion=%s", criterion_id)
                criterion.write({"qc_status": "error", "qc_feedback": "Bedrock call failed"})
                return

            t_in = usage.get("input_tokens", 0)
            t_out = usage.get("output_tokens", 0)
            if t_in or t_out:
                task.sudo().write({
                    "rubric_qc_input_tokens": (task.rubric_qc_input_tokens or 0) + t_in,
                    "rubric_qc_output_tokens": (task.rubric_qc_output_tokens or 0) + t_out,
                })

            verdict_match = re.search(
                r"Rubric\s+QC\s+Verdict:\s*(PASS|FAIL)", response_text, re.IGNORECASE
            )
            check_rows = re.findall(
                r"\|\s*(?:M?\d+)\s*\|[^|]+\|\s*(PASS|FAIL)\s*\|([^|]*)\|",
                response_text, re.IGNORECASE,
            )

            fails = sum(1 for r, _ in check_rows if r.strip().upper() == "FAIL")
            overall = verdict_match.group(1).strip().upper() if verdict_match else ""

            if overall == "PASS" and fails == 0:
                severity = "low"
            elif fails <= 2:
                severity = "medium"
            elif fails <= 5:
                severity = "high"
            else:
                severity = "critical"

            issues_match = re.search(
                r"### Issues.*?\n((?:\d+\..*\n?)+)", response_text
            )
            feedback_parts = []
            if overall:
                feedback_parts.append("Verdict: %s (%d/%d checks failed)" % (
                    overall, fails, len(check_rows),
                ))
            if issues_match:
                feedback_parts.append(issues_match.group(1).strip())

            feedback = "\n".join(feedback_parts) if feedback_parts else response_text[:2000]

            for c in all_criteria:
                c.write({
                    "qc_status": "done",
                    "qc_severity": severity,
                    "qc_feedback": feedback,
                })

            if notify_partner_id:
                partner = env["res.partner"].browse(notify_partner_id)
                if partner.exists():
                    env["bus.bus"]._sendone(
                        partner,
                        "atlas/rubric_qc_done",
                        {
                            "task_id": task_id,
                            "criterion_id": criterion_id,
                            "qc_status": criterion.qc_status,
                            "qc_severity": criterion.qc_severity,
                        },
                    )

            _logger.info(
                "Rubric QC bg: done criterion=%s severity=%s",
                criterion_id, criterion.qc_severity,
            )
    except Exception:
        _logger.exception("Rubric QC bg: unhandled error for criterion=%s", criterion_id)


def _call_bedrock_converse(
    api_key,
    inference_arn,
    region,
    system_prompt,
    user_message,
    max_tokens=4096,
    temperature=0.7,
    top_p=0.9,
    timeout=600.0,
):
    url = BEDROCK_CONVERSE_URL.format(
        region=region,
        model_id=quote(inference_arn, safe=""),
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % api_key,
    }

    payload = {
        "messages": [
            {
                "role": "user",
                "content": [{"text": user_message}],
            },
        ],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
            "topP": top_p,
        },
    }
    if system_prompt:
        payload["system"] = [{"text": system_prompt}]

    with httpx.Client(http2=True, timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        error_detail = resp.text[:500]
        _logger.error(
            "Bedrock API returned status %d: %s", resp.status_code, error_detail
        )
        raise RuntimeError(
            "Bedrock API error (HTTP %d): %s" % (resp.status_code, error_detail)
        )

    result = resp.json()

    output_key = "output" if "output" in result else "Output"
    if output_key in result and isinstance(result[output_key], dict):
        err_type = result[output_key].get("__type", "")
        if err_type:
            raise RuntimeError("Bedrock service error: %s" % err_type)

    content_blocks = result.get("output", {}).get("message", {}).get("content", [])
    response_text = ""
    for block in content_blocks:
        if isinstance(block, dict) and "text" in block:
            response_text += block["text"]

    usage_raw = result.get("usage", {})
    usage = {
        "input_tokens": int(usage_raw.get("inputTokens", 0)),
        "output_tokens": int(usage_raw.get("outputTokens", 0)),
    }

    return response_text.strip(), usage


class LlmAssistQc(http.Controller):
    @http.route("/atlas/qc", type="json", auth="user")
    def qc_prompt(
        self, prompt="", previous_turns=None, system_prompt="",
        max_tokens=4096, temperature=0.3, **kw
    ):
        prompt = (prompt or "").strip()
        if not prompt:
            return {"error": "prompt is required"}

        user_message = self._build_qc_user_message(prompt, previous_turns)

        env = _load_dotenv()
        api_key = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        if not api_key:
            return {"error": "AWS_BEARER_TOKEN_BEDROCK not set in .env"}

        ICP = request.env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()
        region = (ICP.get_param("atlas.bedrock_region") or "ap-south-1").strip()

        if not inference_arn:
            return {"error": "Bedrock Inference ARN not configured in Settings > Atlas"}

        if not system_prompt:
            system_prompt = _get_system_prompt()

        try:
            response_text, usage = _call_bedrock_converse(
                api_key=api_key,
                inference_arn=inference_arn,
                region=region,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=int(max_tokens),
                temperature=float(temperature),
            )

            if _is_degenerate(response_text):
                _logger.warning("QC response degenerated, retrying with temperature=0.1")
                response_text, usage = _call_bedrock_converse(
                    api_key=api_key,
                    inference_arn=inference_arn,
                    region=region,
                    system_prompt=system_prompt,
                    user_message=user_message,
                    max_tokens=int(max_tokens),
                    temperature=0.1,
                    top_p=0.7,
                )

        except Exception as e:
            _logger.exception("QC Bedrock call failed")
            return {"error": str(e)[:500]}

        parsed_json = _parse_json_response(response_text)
        qc_verdict = _parse_qc_verdict(response_text)

        result = {
            "success": True,
            "response": response_text,
            "usage": usage,
        }
        if parsed_json is not None:
            result["parsed_json"] = parsed_json
        if qc_verdict is not None:
            result["qc_result"] = qc_verdict

        return result

    @staticmethod
    def _build_qc_user_message(current_prompt, previous_turns=None):
        if not previous_turns:
            return current_prompt

        parts = ["## Previous Conversation Context\n"]
        for i, turn in enumerate(previous_turns, 1):
            if not isinstance(turn, dict):
                continue
            p = (turn.get("prompt") or "").strip()
            r = (turn.get("response") or "").strip()
            if p:
                parts.append("Turn %d — Prompt: %s" % (i, p[:500]))
            if r:
                parts.append("Turn %d — Response: %s" % (i, r[:500]))
        parts.append("\n## Current Prompt to Evaluate\n%s" % current_prompt)
        return "\n".join(parts)

    @http.route("/atlas/rubric/qc", type="json", auth="user")
    def rubric_criterion_qc(self, criterion_id=0, **kw):
        criterion_id = int(criterion_id or 0)
        if not criterion_id:
            return {"error": "criterion_id is required"}

        criterion = request.env["atlas.rubric.criterion"].browse(criterion_id)
        if not criterion.exists():
            return {"error": "Criterion not found"}

        task = criterion.atlas_id
        if not task.exists():
            return {"error": "Task not found for this criterion"}

        glm_sandbox = task.sandbox_ids.filtered(lambda s: s.model_type == "glm")[:1]
        session_id = glm_sandbox.current_session_id if glm_sandbox else ""

        criterion.write({"qc_status": "running", "qc_feedback": False, "qc_severity": False})

        db_name = request.env.cr.dbname
        task_id = task.id
        notify_partner_id = request.env.user.partner_id.id

        def _submit():
            _RUBRIC_QC_POOL.submit(
                _run_rubric_criterion_qc_background,
                db_name,
                criterion_id,
                task_id,
                session_id,
                notify_partner_id,
            )

        request.env.cr.postcommit.add(_submit)

        return {"success": True, "criterion_id": criterion_id}

    @http.route("/atlas/generation/status", type="json", auth="user")
    def generation_status(self, task_id=0, **kw):
        task_id = int(task_id or 0)
        if not task_id:
            return {"error": "task_id is required"}

        task = request.env["atlas.atlas"].browse(task_id)
        if not task.exists():
            return {"error": "Task not found"}

        return {
            "success": True,
            "goal_generation_status": task.goal_generation_status or "idle",
            "rubric_generation_status": task.rubric_generation_status or "idle",
        }

    @http.route(
        "/api/atlas/llm_assist_qc",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def llm_assist_qc_legacy(self, **params):
        try:
            try:
                jdata = request.get_json_data()
            except Exception:
                jdata = {}

            prompt = (jdata.get("prompt") or "").strip()
            if not prompt:
                return request.make_json_response(
                    {"error": "prompt is required", "status": 400}, status=400
                )

            system_prompt = (jdata.get("system_prompt") or "").strip()
            if not system_prompt:
                system_prompt = _get_system_prompt()

            max_tokens = int(jdata.get("max_tokens", 4096))
            temperature = float(jdata.get("temperature", 0.7))

            env = _load_dotenv()
            api_key = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
            if not api_key:
                return request.make_json_response(
                    {
                        "error": "AWS_BEARER_TOKEN_BEDROCK not set in .env",
                        "status": 500,
                    },
                    status=500,
                )

            ICP = request.env["ir.config_parameter"].sudo()
            inference_arn = (ICP.get_param("atlas.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("atlas.bedrock_region") or "ap-south-1").strip()

            if not inference_arn:
                return request.make_json_response(
                    {"error": "Bedrock Inference ARN not configured", "status": 500},
                    status=500,
                )

            response_text, usage = _call_bedrock_converse(
                api_key=api_key,
                inference_arn=inference_arn,
                region=region,
                system_prompt=system_prompt,
                user_message=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )

            parsed_json = _parse_json_response(response_text)

            result = {
                "success": True,
                "status": 200,
                "response": response_text,
                "usage": usage,
            }
            if parsed_json is not None:
                result["parsed_json"] = parsed_json

            return request.make_json_response(result)

        except Exception as e:
            _logger.exception("llm_assist_qc error")
            return request.make_json_response(
                {"error": str(e), "status": 500}, status=500
            )
