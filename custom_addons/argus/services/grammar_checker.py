# -*- coding: utf-8 -*-
"""Kimi K2.5 prompt grammar / quality check — Argus-owned config.

Uses the **same Bedrock-Converse method** that ``task_forge_core``
uses (POST to ``bedrock-runtime.{region}.amazonaws.com/model/{arn}/converse``
with the ``inferenceConfig`` payload shape), but reads its own
``argus.*`` ``ir.config_parameter`` keys so Argus and TaskForge can
be configured independently:

==========================================  ===========================================
``argus.kimi_api_key``                      Bedrock API key — required.
``argus.kimi_model_arn``                    Bedrock inference profile / model ARN — required.
``argus.kimi_aws_region``                   AWS region (default ``us-east-1``).
``argus.grammar_score_threshold``           Approve/Reject gate (default ``70``).
==========================================  ===========================================

The Bedrock call + JSON parse logic is INTENTIONALLY duplicated from
``task_forge_core/services/kimi_client.py`` rather than imported, so
this module stays standalone (no ``depends`` on task_forge_core in
the manifest, no surprise breakage if TaskForge is uninstalled).
The two implementations should track each other on any future
upstream Bedrock API change.
"""

import json
import logging
import re
from urllib.parse import quote

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


_BEDROCK_CONVERSE_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
)
DEFAULT_REGION = "us-east-1"
# 100 = strict mode: only a perfect grammar score auto-approves.
# Operators can lower this in Settings → Argus → Kimi K2.5
# Configuration if they want a more lenient gate.
DEFAULT_THRESHOLD = 100


# Built-in default system prompt — used when
# ``argus.qc_system_prompt`` is unset or blank.  An operator who
# overrides this MUST preserve the JSON-shape instructions at the
# bottom, otherwise ``_parse_json_response`` won't be able to extract
# a usable result and the form will surface a parse error.
DEFAULT_SYSTEM_PROMPT = (
    "You are a strict QC grammar reviewer for AI prompts used in "
    "Instagram-style video generation tasks.  The user gives you ONE "
    "prompt text.\n\n"
    "Respond with EXACTLY this JSON object — no markdown fences, no "
    "prose, no commentary outside the JSON:\n\n"
    "{\n"
    '  "score": <integer 0-100, where 100 means perfect grammar, '
    'clear intent, and no spelling issues>,\n'
    '  "level": <one of "excellent" | "good" | "fair" | "poor">,\n'
    '  "feedback": <one short paragraph explaining the verdict, '
    'mentioning specific issues if any>,\n'
    '  "issues": [<short English description of each grammar / '
    'spelling / clarity issue; empty list when clean>],\n'
    '  "corrected_text": <the same prompt rewritten with fixes, '
    'preserving the original intent>\n'
    "}\n\n"
    "Use the level mapping:\n"
    "  90-100 -> excellent\n"
    "  75-89  -> good\n"
    "  50-74  -> fair\n"
    "  0-49   -> poor"
)


def _level_from_score(score):
    """Server-side fallback when the model omits ``level``."""
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 50:
        return "fair"
    return "poor"


def _parse_json_response(text):
    """Parse Kimi's text response into a dict, tolerating markdown
    fences (``\`\`\`json ... \`\`\``) and chatty preambles.

    Mirrors ``task_forge_core.services.kimi_client.parse_json_response``
    — see the module docstring for why we duplicate.  Returns ``{}``
    when no JSON object can be recovered.
    """
    if not text:
        return {}
    text = text.strip()

    if "```" in text:
        if "```json" in text:
            text = text.split("```json")[-1].split("```")[0]
        else:
            parts = text.split("```")
            if len(parts) > 1:
                text = parts[1]
    text = text.strip()

    try:
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        pass

    # Last-ditch: pull the outermost { ... } block.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {}


class ArgusGrammarChecker(models.AbstractModel):
    _name = "argus.grammar.checker"
    _description = "Argus Kimi K2.5 Grammar Checker"

    # ------------------------------------------------------------------
    # Config (Argus-owned keys)
    # ------------------------------------------------------------------
    @api.model
    def _get_config(self):
        icp = self.env["ir.config_parameter"].sudo()
        custom_prompt = (icp.get_param("argus.qc_system_prompt") or "").strip()
        return {
            "api_key": icp.get_param("argus.kimi_api_key") or "",
            "arn": icp.get_param("argus.kimi_model_arn") or "",
            "region": icp.get_param("argus.kimi_aws_region") or DEFAULT_REGION,
            "threshold": int(
                icp.get_param("argus.grammar_score_threshold")
                or DEFAULT_THRESHOLD
            ),
            # When the operator hasn't customised the QC review
            # instructions, fall back to the built-in default.  Both
            # MUST keep the JSON-shape instructions intact — the
            # response parser relies on the score / level / feedback
            # / issues / corrected_text keys being present.
            "system_prompt": custom_prompt or DEFAULT_SYSTEM_PROMPT,
        }

    @api.model
    def get_threshold(self):
        return self._get_config()["threshold"]

    @api.model
    def is_configured(self):
        cfg = self._get_config()
        return bool(cfg["api_key"] and cfg["arn"])

    # ------------------------------------------------------------------
    # Bedrock Converse call — same method as task_forge_core but with
    # Argus's own config keys.  We avoid importing from task_forge_core
    # so Argus stays standalone (no cross-module ``depends`` required).
    # ------------------------------------------------------------------
    @api.model
    def _bedrock_converse(self, system_prompt, user_text, temperature=0.1):
        cfg = self._get_config()
        if not cfg["api_key"]:
            raise UserError(_(
                "Argus Bedrock API key is not configured.\n\n"
                "Go to Settings → Argus → Kimi K2.5 Configuration "
                "and fill in:\n"
                "  • Bedrock API Key\n"
                "  • Bedrock Model ARN\n"
                "  • AWS Region (default us-east-1)\n\n"
                "Then click Save and re-run QC Prompt."
            ))
        if not cfg["arn"]:
            raise UserError(_(
                "Argus Bedrock Model ARN is not configured.\n\n"
                "Open Settings → Argus → Kimi K2.5 Configuration and "
                "paste your inference-profile / model ARN (it starts "
                "with ``arn:aws:bedrock:...``), then click Save."
            ))

        url = _BEDROCK_CONVERSE_URL.format(
            region=cfg["region"],
            model_id=quote(cfg["arn"], safe=""),
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        }
        payload = {
            "messages": [
                {"role": "user", "content": [{"text": user_text}]},
            ],
            "system": [{"text": system_prompt}],
            "inferenceConfig": {
                "maxTokens": 16384,
                "temperature": temperature,
            },
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
        except requests.RequestException as exc:
            _logger.warning(
                "Argus Bedrock network error url=%s region=%s arn=%s err=%s",
                url, cfg["region"], cfg["arn"], exc,
            )
            raise UserError(_(
                "Could not reach Bedrock.\n\n"
                "URL:    %(url)s\n"
                "Region: %(region)s\n"
                "ARN:    %(arn)s\n\n"
                "Error: %(err)s\n\n"
                "Check the AWS region, your network, and your API key "
                "under Settings → Argus → Kimi K2.5 Configuration."
            ) % {
                "url": url, "region": cfg["region"], "arn": cfg["arn"], "err": exc,
            })

        if resp.status_code != 200:
            _logger.error(
                "Argus Bedrock HTTP %d url=%s region=%s arn=%s body=%s",
                resp.status_code, url, cfg["region"], cfg["arn"],
                resp.text[:500],
            )
            # Region-mismatch hint: a real ARN is 6 colon-separated
            # segments, where the 4th segment is the region.  When
            # that doesn't match argus.kimi_aws_region, Bedrock's
            # "invalid model identifier" error is almost always
            # because the call went to the wrong region.
            arn_region_hint = ""
            arn_parts = (cfg["arn"] or "").split(":")
            if (
                len(arn_parts) >= 4
                and arn_parts[3]
                and cfg["region"]
                and arn_parts[3] != cfg["region"]
            ):
                arn_region_hint = _(
                    "\n\nHINT: your ARN's region is '%(arn_region)s' but "
                    "the Argus AWS Region setting is '%(cfg_region)s'.  "
                    "These must match.  Open Settings → Argus → Kimi K2.5 "
                    "Configuration and set the AWS Region to "
                    "'%(arn_region)s', then Save and retry."
                ) % {
                    "arn_region": arn_parts[3],
                    "cfg_region": cfg["region"],
                }
            # Shape-of-ARN hint: very common to paste a model ID
            # (``anthropic.claude-...``) into the ARN slot.  Bedrock
            # Converse accepts BOTH but only when the URL is built
            # accordingly; when an ARN-shaped value is missing the
            # leading ``arn:`` we still treat it as an ID — but if
            # someone pasted half an ARN this hint flags it.
            if cfg["arn"] and not cfg["arn"].startswith("arn:") and ":" in cfg["arn"] and "/" in cfg["arn"]:
                arn_region_hint += _(
                    "\n\nHINT: your Bedrock Model ARN looks malformed — "
                    "it should start with 'arn:aws:bedrock:...' or be "
                    "a plain model id like 'anthropic.claude-3-5-sonnet-20241022-v2:0'."
                )
            raise UserError(_(
                "Bedrock API returned HTTP %(code)s: %(body)s\n\n"
                "URL:    %(url)s\n"
                "Region: %(region)s\n"
                "ARN:    %(arn)s%(hint)s"
            ) % {
                "code": resp.status_code,
                "body": (resp.text or "")[:500],
                "url": url,
                "region": cfg["region"],
                "arn": cfg["arn"],
                "hint": arn_region_hint,
            })

        data = resp.json()
        message = data.get("output", {}).get("message", {})
        blocks = message.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        usage = data.get("usage") or {}
        return {
            "text": text,
            "usage": {
                "input_tokens": usage.get("inputTokens", 0),
                "output_tokens": usage.get("outputTokens", 0),
            },
        }

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    @api.model
    def check(self, text):
        """Grammar-check ``text`` via Kimi K2.5 on AWS Bedrock.

        Returns a dict with keys:

        * ``score`` — int 0..100
        * ``level`` — ``excellent / good / fair / poor``
        * ``feedback`` — one-paragraph verdict
        * ``issues`` — list[str]
        * ``corrected_text`` — Kimi's rewrite
        * ``raw_json`` — pretty-printed parsed payload (stored on
          the task for audit)
        * ``threshold`` — int (the score gate in effect for this call)
        * ``usage`` — Bedrock token-usage dict

        Raises :class:`~odoo.exceptions.UserError` on any failure so
        the form button surfaces the cause cleanly.
        """
        text = (text or "").strip()
        if not text:
            raise UserError(_(
                "Task prompt is empty — nothing to grammar-check.  "
                "Fill in the Prompt field first."
            ))

        # Pull the system prompt from config so the operator can
        # tweak the review instructions via Settings → Argus → Kimi
        # K2.5 Configuration → "QC system prompt".  Blank config
        # falls back to ``DEFAULT_SYSTEM_PROMPT``.
        system_prompt = self._get_config()["system_prompt"]
        result = self._bedrock_converse(system_prompt, text, temperature=0.1)
        raw_text = (result.get("text") or "").strip()
        parsed = _parse_json_response(raw_text)

        if not parsed:
            raise UserError(_(
                "Kimi response could not be parsed as JSON.  Raw text:\n\n%s"
            ) % raw_text[:1000])

        try:
            score = int(parsed.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))

        level = (parsed.get("level") or "").strip().lower()
        if level not in ("excellent", "good", "fair", "poor"):
            level = _level_from_score(score)

        pretty_json = json.dumps(parsed, indent=2, ensure_ascii=False)

        return {
            "score": score,
            "level": level,
            "feedback": str(parsed.get("feedback") or "").strip(),
            "issues": [str(i).strip() for i in (parsed.get("issues") or []) if i],
            "corrected_text": str(parsed.get("corrected_text") or "").strip(),
            "raw_json": pretty_json,
            "threshold": self.get_threshold(),
            "usage": result.get("usage") or {},
        }
