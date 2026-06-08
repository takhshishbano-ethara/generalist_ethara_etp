"""LLM token pricing — DB-backed with sane defaults.

Rates live in `ir.config_parameter` under the `crowley_ext.pricing.*` namespace.
On first read (or when a key is unset), `DEFAULT_PRICING_PER_MTOK` is used as a
fallback so the system works out of the box.

The refresh endpoint (see `pricing_refresh.py`) can overwrite these from a live
source — vendor pricing changes are reflected without a code deploy.

Public surface:
    DEFAULT_PRICING_PER_MTOK   — fallback constants (copied from kensei docstring)
    PREFIXES                   — ordered list of all known prefix keys
    cost_for_pair(prefix, in, out, rates) — compute USD cost for one prefix
    get_pricing(env)            — effective rates from DB+default fallback
    set_pricing(env, rates)     — write rates to ir.config_parameter
    get_fx_usd_inr(env)         — current USD→INR rate (None if unset)
    set_fx_usd_inr(env, rate)   — write FX rate to ir.config_parameter
    get_last_updated(env)       — pricing/FX last-updated ISO strings
"""

from odoo import fields

DEFAULT_PRICING_PER_MTOK = {
    "claude":     (5.00, 25.00),
    "gpt":        (5.00, 30.00),
    "glm":        (0.60, 3.00),
    "kimi_eval":  (0.60, 3.00),
    "oneP":   (5.00, 25.00),
    "onePA":  (5.00, 25.00),
    "onePB":  (5.00, 25.00),
    "onePC":  (5.00, 25.00),
    "onePD":  (5.00, 25.00),
    "bedrock":  (5.00, 25.00),
    "traj_qc":  (0.60, 3.00),
    "taskdesc": (5.00, 25.00),
    "golden":   (5.00, 25.00),
}

PREFIXES = list(DEFAULT_PRICING_PER_MTOK.keys())

PARAM_PRICING_PREFIX = "crowley_ext.pricing"
PARAM_PRICING_LAST = f"{PARAM_PRICING_PREFIX}.last_updated"
PARAM_FX_USD_INR = "crowley_ext.fx.usd_inr"
PARAM_FX_LAST = "crowley_ext.fx.last_updated"


def _param_keys(prefix):
    return (
        f"{PARAM_PRICING_PREFIX}.{prefix}.input_per_mtok",
        f"{PARAM_PRICING_PREFIX}.{prefix}.output_per_mtok",
    )


def _f(value, default):
    if value in (None, "", False):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_pricing(env):
    """Return {prefix: (input_$/Mtok, output_$/Mtok)} using DB values when set,
    falling back to DEFAULT_PRICING_PER_MTOK otherwise."""
    Param = env["ir.config_parameter"].sudo()
    out = {}
    for prefix, (def_in, def_out) in DEFAULT_PRICING_PER_MTOK.items():
        in_key, out_key = _param_keys(prefix)
        out[prefix] = (
            _f(Param.get_param(in_key), def_in),
            _f(Param.get_param(out_key), def_out),
        )
    return out


def set_pricing(env, rates):
    """Persist `rates = {prefix: (in_$/Mtok, out_$/Mtok)}` and stamp last_updated."""
    Param = env["ir.config_parameter"].sudo()
    for prefix, (in_r, out_r) in rates.items():
        in_key, out_key = _param_keys(prefix)
        Param.set_param(in_key, str(float(in_r)))
        Param.set_param(out_key, str(float(out_r)))
    Param.set_param(PARAM_PRICING_LAST, fields.Datetime.now().isoformat())


def get_fx_usd_inr(env):
    """Return current USD→INR rate, or None if never set."""
    Param = env["ir.config_parameter"].sudo()
    raw = Param.get_param(PARAM_FX_USD_INR)
    val = _f(raw, 0.0)
    return val if val > 0 else None


def set_fx_usd_inr(env, rate):
    Param = env["ir.config_parameter"].sudo()
    Param.set_param(PARAM_FX_USD_INR, str(float(rate)))
    Param.set_param(PARAM_FX_LAST, fields.Datetime.now().isoformat())


def get_last_updated(env):
    Param = env["ir.config_parameter"].sudo()
    return {
        "pricing_last_updated": Param.get_param(PARAM_PRICING_LAST) or "",
        "fx_last_updated": Param.get_param(PARAM_FX_LAST) or "",
    }


def cost_for_pair(prefix, input_tokens, output_tokens, rates=None):
    """USD cost for one prefix's token pair.

    Pass `rates = get_pricing(env)` once per request to use the DB-backed table.
    If `rates` is None, falls back to `DEFAULT_PRICING_PER_MTOK`.
    """
    table = rates if rates is not None else DEFAULT_PRICING_PER_MTOK
    inp_rate, out_rate = table.get(prefix, (0.0, 0.0))
    inp_cost = (input_tokens or 0) * inp_rate / 1_000_000.0
    out_cost = (output_tokens or 0) * out_rate / 1_000_000.0
    return inp_cost, out_cost, inp_cost + out_cost
