# -*- coding: utf-8 -*-
import json
import logging
import os
import re
from urllib.parse import quote

import httpx

from odoo import http
from odoo.http import request
from odoo.modules.module import get_module_path

from ..models.talos import _load_dotenv

_logger = logging.getLogger(__name__)

BEDROCK_CONVERSE_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
)

_system_prompt_cache = None
_trajectory_qc_prompt_cache = None


def _get_system_prompt():
    global _system_prompt_cache
    if _system_prompt_cache is not None:
        return _system_prompt_cache

    mod_path = get_module_path("talos")
    if not mod_path:
        return ""

    path = os.path.join(mod_path, "system_prompts.md")
    if os.path.isfile(path):
        with open(path, "r") as f:
            _system_prompt_cache = f.read().strip()
    else:
        _system_prompt_cache = ""

    return _system_prompt_cache


def _get_trajectory_qc_prompt():
    global _trajectory_qc_prompt_cache
    if _trajectory_qc_prompt_cache is not None:
        return _trajectory_qc_prompt_cache

    mod_path = get_module_path("talos")
    if not mod_path:
        return ""

    path = os.path.join(mod_path, "trajectory_qc_prompt.md")
    if os.path.isfile(path):
        with open(path, "r") as f:
            _trajectory_qc_prompt_cache = f.read().strip()
    else:
        _trajectory_qc_prompt_cache = ""

    return _trajectory_qc_prompt_cache


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
    """Parse the machine-readable JSON block from the QC response.

    The LLM outputs a JSON code block with severity, summary, and per-check
    results.  We extract the JSON, validate the severity field, and return a
    normalized dict the frontend can consume directly.
    """
    parsed = _parse_json_response(text)
    if not parsed or not isinstance(parsed, dict):
        return None

    severity = (parsed.get("severity") or "").strip().lower()
    valid_severities = ("low", "medium", "high", "critical")
    if severity not in valid_severities:
        # Fallback: try to infer from old-style OVERALL VERDICT if present
        verdict_match = re.search(
            r"OVERALL\s+(?:VERDICT|SEVERITY):\s*(\S+)", text, re.IGNORECASE
        )
        if verdict_match:
            raw = verdict_match.group(1).strip().lower()
            if raw in valid_severities:
                severity = raw
            elif raw == "pass":
                severity = "low"
            elif raw == "warn":
                severity = "medium"
            elif raw == "fail":
                severity = "critical"
            else:
                severity = "medium"
        else:
            severity = "medium"

    total_fails = int(parsed.get("total_fails", 0))
    total_warns = int(parsed.get("total_warns", 0))
    total_passes = int(parsed.get("total_passes", 0))

    return {
        "severity": severity,
        "summary": parsed.get("summary", ""),
        "total_fails": total_fails,
        "total_warns": total_warns,
        "total_passes": total_passes,
        "checks": parsed.get("checks", []),
    }


def _is_degenerate(text, threshold=0.8):
    """Detect repetitive token degeneration (e.g. '!!!...', 'aaa...').

    Returns True when any single character makes up more than *threshold*
    of the response — a strong signal the model collapsed into a loop.
    """
    if not text or len(text) < 20:
        return False
    from collections import Counter

    counts = Counter(text)
    most_common_count = counts.most_common(1)[0][1]
    return most_common_count / len(text) >= threshold


def _call_bedrock_converse(
    api_key,
    inference_arn,
    region,
    system_prompt,
    user_message,
    max_tokens=4096,
    temperature=0.7,
    top_p=0.9,
    timeout=120.0,
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

    with httpx.Client(http2=False, timeout=timeout) as client:
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
    @http.route("/talos/qc", type="json", auth="user")
    def qc_prompt(
        self, prompt="", system_prompt="", max_tokens=4096, temperature=0.3, **kw
    ):
        prompt = (prompt or "").strip()
        if not prompt:
            return {"error": "prompt is required"}

        env = _load_dotenv()
        api_key = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        if not api_key:
            return {"error": "AWS_BEARER_TOKEN_BEDROCK not set in .env"}

        ICP = request.env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("talos.bedrock_inference_arn") or "").strip()
        region = (ICP.get_param("talos.bedrock_region") or "ap-south-1").strip()

        if not inference_arn:
            return {"error": "Bedrock Inference ARN not configured in Settings > Talos"}

        if not system_prompt:
            system_prompt = _get_system_prompt()

        try:
            response_text, usage = _call_bedrock_converse(
                api_key=api_key,
                inference_arn=inference_arn,
                region=region,
                system_prompt=system_prompt,
                user_message=prompt,
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
                    user_message=prompt,
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

    @http.route("/talos/trajectory_qc", type="json", auth="user")
    def trajectory_qc(self, trajectory="", max_tokens=4096, temperature=0.3, **kw):
        """Run QC evaluation on a single trajectory session."""
        trajectory = (trajectory or "").strip()
        if not trajectory:
            return {"error": "trajectory is required"}

        env = _load_dotenv()
        api_key = env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        if not api_key:
            return {"error": "AWS_BEARER_TOKEN_BEDROCK not set in .env"}

        ICP = request.env["ir.config_parameter"].sudo()
        inference_arn = (ICP.get_param("talos.bedrock_inference_arn") or "").strip()
        region = (ICP.get_param("talos.bedrock_region") or "ap-south-1").strip()

        if not inference_arn:
            return {"error": "Bedrock Inference ARN not configured in Settings > Talos"}

        system_prompt = _get_trajectory_qc_prompt()
        if not system_prompt:
            return {"error": "trajectory_qc_prompt.md not found"}

        if len(trajectory) > 50000:
            trajectory = trajectory[:50000] + "\n\n[... truncated for length ...]"

        try:
            response_text, usage = _call_bedrock_converse(
                api_key=api_key,
                inference_arn=inference_arn,
                region=region,
                system_prompt=system_prompt,
                user_message=trajectory,
                max_tokens=int(max_tokens),
                temperature=float(temperature),
            )

            if _is_degenerate(response_text):
                _logger.warning(
                    "Trajectory QC response degenerated, retrying with temperature=0.1"
                )
                response_text, usage = _call_bedrock_converse(
                    api_key=api_key,
                    inference_arn=inference_arn,
                    region=region,
                    system_prompt=system_prompt,
                    user_message=trajectory,
                    max_tokens=int(max_tokens),
                    temperature=0.1,
                    top_p=0.7,
                )

        except Exception as e:
            _logger.exception("Trajectory QC Bedrock call failed")
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

    @http.route(
        "/api/talos/llm_assist_qc",
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
            inference_arn = (ICP.get_param("talos.bedrock_inference_arn") or "").strip()
            region = (ICP.get_param("talos.bedrock_region") or "ap-south-1").strip()

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
