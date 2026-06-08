"""Live-pricing fetch/store controller.

Two endpoints, both gated to CTO / TPM (Technical):

    GET  /api/v1/crowley_extension/llm_pricing
         → current effective rates + last-updated timestamps

    POST /api/v1/crowley_extension/llm_pricing/refresh
         → fetch fresh USD→INR (Frankfurter) and LLM per-token rates (LiteLLM),
           write into ir.config_parameter, return the resulting table

No cron involved — frontend hits /refresh on user click.
"""

import json
import logging

import requests

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .llm_cost_dashboard import _require_cto_or_tpm
from . import pricing as P

_logger = logging.getLogger(__name__)

FX_URL = "https://api.frankfurter.app/latest?from=USD&to=INR"
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
HTTP_TIMEOUT_S = 8.0

# Map our prefix -> the LiteLLM model key that carries the canonical rate.
# Derivative prefixes (bedrock, taskdesc, golden, oneP*) follow the primary
# they're routed through — see comments in pricing.DEFAULT_PRICING_PER_MTOK.
LITELLM_PRIMARY_FOR = {
    "claude":     "claude-opus-4-7",
    "gpt":        "gpt-5",
    "glm":        "glm-4.5",
    "kimi_eval":  "moonshot/moonshot-v1-8k",
    "oneP":       "claude-opus-4-7",
    "onePA":      "claude-opus-4-7",
    "onePB":      "claude-opus-4-7",
    "onePC":      "claude-opus-4-7",
    "onePD":      "claude-opus-4-7",
    "bedrock":    "claude-opus-4-7",
    "traj_qc":    "glm-4.5",
    "taskdesc":   "claude-opus-4-7",
    "golden":     "claude-opus-4-7",
}


def _fetch_json(url):
    """GET a URL with a short timeout, return parsed JSON. Raises on failure."""
    resp = requests.get(url, timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def _refresh_fx(env):
    try:
        data = _fetch_json(FX_URL)
    except Exception as exc:
        _logger.warning("FX refresh failed: %s", exc)
        return {"ok": False, "source": "frankfurter", "error": str(exc)}
    rate = (data.get("rates") or {}).get("INR")
    if not rate:
        return {"ok": False, "source": "frankfurter", "error": "INR not in response"}
    P.set_fx_usd_inr(env, float(rate))
    return {
        "ok": True,
        "source": "frankfurter",
        "fetched_at": data.get("date") or "",
        "usd_to_inr": float(rate),
    }


def _refresh_pricing(env):
    try:
        catalogue = _fetch_json(LITELLM_URL)
    except Exception as exc:
        _logger.warning("LiteLLM pricing refresh failed: %s", exc)
        return {"ok": False, "source": "litellm", "error": str(exc)}

    new_rates = {}
    unresolved = []
    for prefix, primary_key in LITELLM_PRIMARY_FOR.items():
        entry = catalogue.get(primary_key) or {}
        in_per_token = entry.get("input_cost_per_token")
        out_per_token = entry.get("output_cost_per_token")
        if in_per_token is None or out_per_token is None:
            unresolved.append({"prefix": prefix, "litellm_key": primary_key})
            new_rates[prefix] = P.DEFAULT_PRICING_PER_MTOK[prefix]
            continue
        new_rates[prefix] = (
            float(in_per_token) * 1_000_000.0,
            float(out_per_token) * 1_000_000.0,
        )

    P.set_pricing(env, new_rates)
    return {
        "ok": True,
        "source": "litellm",
        "resolved": len(new_rates) - len(unresolved),
        "unresolved": unresolved,
    }


def _serialize_rates(rates, fx_usd_inr):
    rows = []
    for prefix, (in_r, out_r) in rates.items():
        rows.append({
            "prefix": prefix,
            "input_usd_per_mtok": round(in_r, 6),
            "output_usd_per_mtok": round(out_r, 6),
            "input_inr_per_mtok": round(in_r * fx_usd_inr, 4) if fx_usd_inr else None,
            "output_inr_per_mtok": round(out_r * fx_usd_inr, 4) if fx_usd_inr else None,
            "primary_litellm_key": LITELLM_PRIMARY_FOR.get(prefix),
        })
    return rows


class CrowleyExtensionLLMPricing(http.Controller):

    @http.route(
        "/api/v1/crowley_extension/llm_pricing",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def llm_pricing_show(self, **kwargs):
        env = request.env

        guard = _require_cto_or_tpm(env)
        if guard is not None:
            return guard

        rates = P.get_pricing(env)
        fx = P.get_fx_usd_inr(env)
        meta = P.get_last_updated(env)

        data = {
            "fx_usd_inr": fx,
            "pricing_last_updated": meta["pricing_last_updated"],
            "fx_last_updated": meta["fx_last_updated"],
            "rates": _serialize_rates(rates, fx),
        }
        return return_Response(message="OK", status=200, data=data)

    @http.route(
        "/api/v1/crowley_extension/llm_pricing/refresh",
        type="http",
        auth="none",
        methods=["POST", "GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def llm_pricing_refresh(self, **kwargs):
        env = request.env

        guard = _require_cto_or_tpm(env)
        if guard is not None:
            return guard

        fx_result = _refresh_fx(env)
        pricing_result = _refresh_pricing(env)

        rates = P.get_pricing(env)
        fx = P.get_fx_usd_inr(env)
        meta = P.get_last_updated(env)

        data = {
            "fx_refresh": fx_result,
            "pricing_refresh": pricing_result,
            "fx_usd_inr": fx,
            "pricing_last_updated": meta["pricing_last_updated"],
            "fx_last_updated": meta["fx_last_updated"],
            "rates": _serialize_rates(rates, fx),
        }
        success = fx_result.get("ok") and pricing_result.get("ok")
        return return_Response(
            message="OK" if success else "Partial refresh — see fx_refresh / pricing_refresh",
            status=200 if success else 207,
            data=data,
        )
