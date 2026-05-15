# -*- coding: utf-8 -*-
"""Kimi K2.5 grammar-check service.

Thin synchronous adapter over Moonshot AI's OpenAI-compatible chat
completions endpoint.  Called by the QC review wizard to grade the
grammar of the version's prompt and the reviewer's suggested next
prompt before an approve verdict can be issued.

The endpoint, model, API key, and score threshold are all
configurable via ``ir.config_parameter`` so an operator can pivot to
a different host (Moonshot CN vs INT) or model variant without code
changes::

    video_qc.kimi_api_key             = sk-...
    video_qc.kimi_endpoint            = https://api.moonshot.ai/v1/chat/completions
    video_qc.kimi_model               = kimi-k2-0905-preview
    video_qc.grammar_score_threshold  = 70

The service is deliberately a thin wrapper — it sends a strict
JSON-mode request, parses the response, and surfaces clear errors
back to the wizard.  We do NOT cache responses (a re-run after a
prompt edit should hit a fresh API call) and we do NOT retry on
non-2xx (any rate-limit / quota issue should be visible to the
reviewer, not silently swallowed).
"""

import json
import logging

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


DEFAULT_ENDPOINT = "https://api.moonshot.ai/v1/chat/completions"
DEFAULT_MODEL = "kimi-k2-0905-preview"
DEFAULT_THRESHOLD = 70

# The system prompt is deliberately verbose: we tell Kimi exactly
# what JSON shape we expect so we can parse without heuristics.  The
# ``response_format`` kwarg below also forces JSON output.
_SYSTEM_PROMPT = (
    "You are a strict but fair grammar reviewer for marketing copy "
    "and AI image / video prompts written in English.  You receive "
    "one piece of text from the user.\n\n"
    "Respond with ONE JSON object, no prose, no markdown, with EXACTLY "
    "these keys:\n\n"
    "{\n"
    '  "score": <integer 0-100, where 100 means perfect grammar and '
    'fluent prose>,\n'
    '  "summary": <one-sentence English summary of the text\'s '
    'grammar quality>,\n'
    '  "issues": [<short English description of each grammar / '
    'spelling / punctuation issue, one entry per problem; empty list '
    'when the text is clean>],\n'
    '  "corrected_text": <the same text rewritten with all grammar '
    'issues fixed, preserving the original meaning and tone>\n'
    "}\n\n"
    "Do not include any markdown fences, prose, or commentary outside "
    "the JSON object."
)


class KimiGrammarChecker(models.AbstractModel):
    _name = "video.qc.grammar.checker"
    _description = "Kimi K2.5 Grammar Check Service"

    # ------------------------------------------------------------------
    # Config accessors
    # ------------------------------------------------------------------
    @api.model
    def _get_config(self):
        icp = self.env["ir.config_parameter"].sudo()
        return {
            "api_key": icp.get_param("video_qc.kimi_api_key") or "",
            "endpoint": icp.get_param("video_qc.kimi_endpoint") or DEFAULT_ENDPOINT,
            "model": icp.get_param("video_qc.kimi_model") or DEFAULT_MODEL,
            "threshold": int(
                icp.get_param("video_qc.grammar_score_threshold")
                or DEFAULT_THRESHOLD
            ),
        }

    @api.model
    def get_threshold(self):
        """Return the configured grammar-score threshold."""
        return self._get_config()["threshold"]

    @api.model
    def is_configured(self):
        """True iff an API key is set — callers use this to decide
        whether the Check Grammar button should be enabled."""
        return bool(self._get_config()["api_key"])

    # ------------------------------------------------------------------
    # The actual call
    # ------------------------------------------------------------------
    @api.model
    def check(self, text):
        """Grammar-check ``text`` via Moonshot Kimi.

        Returns a dict with keys ``score`` (int 0-100), ``summary``
        (str), ``issues`` (list[str]), ``corrected_text`` (str).
        Raises :class:`~odoo.exceptions.UserError` on configuration,
        network, or response-parsing failure.
        """
        if not text or not text.strip():
            # Empty input is technically perfect grammar; short-circuit
            # so we don't burn API quota on no-ops.
            return {
                "score": 100,
                "summary": _("Empty text — nothing to grade."),
                "issues": [],
                "corrected_text": "",
            }

        cfg = self._get_config()
        if not cfg["api_key"]:
            raise UserError(_(
                "Kimi API key is not configured.\n\n"
                "Set it via Settings → Technical → Parameters → System "
                "Parameters:\n"
                "  video_qc.kimi_api_key = <your Moonshot AI key>\n\n"
                "Optional overrides:\n"
                "  video_qc.kimi_endpoint (default %(endpoint)s)\n"
                "  video_qc.kimi_model (default %(model)s)"
            ) % {"endpoint": DEFAULT_ENDPOINT, "model": DEFAULT_MODEL})

        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0.1,           # near-deterministic grading
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                cfg["endpoint"],
                json=payload,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            _logger.warning("Kimi grammar-check network error: %s", exc)
            raise UserError(_(
                "Could not reach the Kimi API (%(endpoint)s): %(err)s.\n\n"
                "Check the endpoint URL, your network, and your API key."
            ) % {"endpoint": cfg["endpoint"], "err": exc})

        if response.status_code != 200:
            # Surface the body so the reviewer sees auth / quota / model
            # errors directly — that's what Moonshot returns there.
            raise UserError(_(
                "Kimi API returned HTTP %(code)s: %(body)s"
            ) % {
                "code": response.status_code,
                "body": (response.text or "")[:1000],
            })

        try:
            outer = response.json()
            content = outer["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (ValueError, KeyError, IndexError) as exc:
            _logger.warning(
                "Kimi grammar-check unexpected response shape: %s; body=%s",
                exc, response.text[:1000],
            )
            raise UserError(_(
                "Kimi response could not be parsed (%(err)s).  Raw body "
                "below — please share with the dev team if this "
                "keeps happening.\n\n%(body)s"
            ) % {"err": exc, "body": response.text[:1000]})

        # Coerce + clamp.  We don't trust the model to honor the
        # 0-100 contract perfectly.
        try:
            score = int(parsed.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))

        return {
            "score": score,
            "summary": str(parsed.get("summary") or "").strip(),
            "issues": [str(it).strip() for it in (parsed.get("issues") or []) if it],
            "corrected_text": str(parsed.get("corrected_text") or "").strip(),
        }
