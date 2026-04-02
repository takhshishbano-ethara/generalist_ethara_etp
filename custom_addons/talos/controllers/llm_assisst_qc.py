# -*- coding: utf-8 -*-
import json
import logging
import os
import re
from urllib.parse import quote

import httpx

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_env_loaded = False


def _load_env():
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        from dotenv import load_dotenv

        addons_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        custom_addons_dir = os.path.dirname(addons_dir)
        env_path = os.path.join(custom_addons_dir, ".env")
        if os.path.isfile(env_path):
            load_dotenv(env_path)
            _logger.info("talos: loaded .env from %s", env_path)
        else:
            load_dotenv()
    except ImportError:
        pass


def _get_bedrock_api_key():
    _load_env()
    return os.environ.get("TALOS_BEDROCK_API_KEY", "").strip()


def _get_bedrock_config_from_odoo():
    ICP = request.env["ir.config_parameter"].sudo()
    inference_arn = (ICP.get_param("talos.bedrock_inference_arn") or "").strip()
    region = (ICP.get_param("talos.bedrock_region") or "ap-south-1").strip()
    return inference_arn, region


BEDROCK_CONVERSE_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
)


def _build_endpoint(region, inference_arn):
    return BEDROCK_CONVERSE_URL.format(
        region=region,
        model_id=quote(inference_arn, safe=""),
    )


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


def _call_bedrock_converse(
    api_key,
    inference_arn,
    region,
    system_prompt,
    user_message,
    max_tokens=4096,
    temperature=0.7,
):
    url = _build_endpoint(region, inference_arn)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
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
        },
    }
    if system_prompt:
        payload["system"] = [{"text": system_prompt}]

    with httpx.Client(http2=True, timeout=120.0) as client:
        resp = client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        error_detail = resp.text[:500]
        _logger.error(
            "Bedrock API returned status %d: %s", resp.status_code, error_detail
        )
        raise RuntimeError(
            f"Bedrock API error (HTTP {resp.status_code}): {error_detail}"
        )

    result = resp.json()

    output_key = "output" if "output" in result else "Output"
    if output_key in result and isinstance(result[output_key], dict):
        err_type = result[output_key].get("__type", "")
        if err_type:
            raise RuntimeError(f"Bedrock service error: {err_type}")

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
    @http.route(
        "/api/talos/llm_assist_qc",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def llm_assist_qc(self, **params):
        try:
            try:
                jdata = request.get_json_data()
            except Exception:
                jdata = {}

            prompt = (jdata.get("prompt") or "").strip()
            if not prompt:
                return request.make_json_response(
                    {"error": "prompt is required", "status": 400},
                    status=400,
                )

            system_prompt = (jdata.get("system_prompt") or "").strip()
            max_tokens = int(jdata.get("max_tokens", 4096))
            temperature = float(jdata.get("temperature", 0.7))

            api_key = _get_bedrock_api_key()
            if not api_key:
                return request.make_json_response(
                    {
                        "error": "TALOS_BEDROCK_API_KEY not configured in .env",
                        "status": 500,
                    },
                    status=500,
                )

            inference_arn, region = _get_bedrock_config_from_odoo()
            if not inference_arn:
                return request.make_json_response(
                    {
                        "error": "Bedrock Inference ARN not configured in Settings > Talos",
                        "status": 500,
                    },
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
                {"error": str(e), "status": 500},
                status=500,
            )
