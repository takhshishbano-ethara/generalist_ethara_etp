"""Cross-project LLM cost summary + drill-down endpoints.

Aggregates spend across the five projects that persist token or cost data:
- kensei, talos: tokens only -> cost computed via `pricing.PRICING_PER_MTOK`
- skoll, kaiju, crowley: cost already stored -> read directly

Endpoints:
- GET /api/v1/crowley_extension/llm_cost_summary
- GET /api/v1/crowley_extension/llm_cost_breakdown

Both accept ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD for windowing.
"""

from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .analytics_dashboard import FULL_ACCESS_ROLE_XMLIDS, _get_role_ids
from .pricing import (
    PREFIXES,
    cost_for_pair,
    get_fx_usd_inr,
    get_last_updated,
    get_pricing,
)

VALID_PROJECTS = ("kensei", "talos", "skoll", "kaiju", "crowley")
BREAKDOWN_DEFAULT_LIMIT = 50
BREAKDOWN_MAX_LIMIT = 500


def _require_cto_or_tpm(env):
    role = env.user.user_role
    if role and role.id in _get_role_ids(env, FULL_ACCESS_ROLE_XMLIDS):
        return None
    return return_Response(
        message="Only CTO or TPM (Technical) can access the LLM cost endpoints.",
        status=403,
    )


def _round6(value):
    return round(float(value or 0.0), 6)


def _to_inr(usd, fx):
    """Multiply usd by fx if fx is set, else return None (frontend can hide)."""
    if not fx or not usd:
        return 0.0 if fx and not usd else None if not fx else 0.0
    return round(float(usd) * float(fx), 2)


def _amounts(usd, fx):
    """Return a (usd, inr) pair for response payloads."""
    return _round6(usd), _to_inr(usd, fx)


def _parse_date(raw, label):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, return_Response(
            message=f"Invalid {label} '{raw}'. Expected YYYY-MM-DD.",
            status=400,
        )


def _parse_date_filters(params):
    """Return (date_domain, error_response).

    date_domain is a list of leaves to AND into search domains, e.g.
    [("create_date", ">=", <dt>), ("create_date", "<", <dt>)].
    """
    domain = []
    raw_start = (params.get("start_date") or "").strip()
    raw_end = (params.get("end_date") or "").strip()

    start = end = None
    if raw_start:
        start, err = _parse_date(raw_start, "start_date")
        if err is not None:
            return None, err
    if raw_end:
        end, err = _parse_date(raw_end, "end_date")
        if err is not None:
            return None, err
    if start and end and start > end:
        return None, return_Response(
            message="Invalid date range: start_date must be on or before end_date.",
            status=400,
        )

    if start:
        domain.append(
            ("create_date", ">=", datetime.combine(start, datetime.min.time()))
        )
    if end:
        domain.append(
            ("create_date", "<",
             datetime.combine(end, datetime.min.time()) + timedelta(days=1))
        )
    return domain, None


def _coerce_int(value, default):
    try:
        result = int(value)
        return result if result >= 0 else default
    except (TypeError, ValueError):
        return default


def _iso(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ─────────────────────────── aggregators (with date domain) ───────────────────


def _sum_token_pair(Task, prefix, domain):
    in_field = f"{prefix}_input_tokens"
    out_field = f"{prefix}_output_tokens"
    if in_field not in Task._fields or out_field not in Task._fields:
        return None
    rows = Task._read_group(
        domain, [], [f"{in_field}:sum", f"{out_field}:sum"]
    )
    if not rows:
        return 0, 0
    in_sum, out_sum = rows[0]
    return int(in_sum or 0), int(out_sum or 0)


def _kensei_or_talos_cost(env, model_name, project_label, date_domain,
                          rates, fx):
    Task = env.get(model_name)
    if Task is None:
        return None
    Task = Task.sudo()

    by_model = []
    total = 0.0
    for prefix in PREFIXES:
        pair = _sum_token_pair(Task, prefix, date_domain)
        if pair is None:
            continue
        in_tok, out_tok = pair
        if in_tok == 0 and out_tok == 0:
            continue
        in_cost, out_cost, sub_total = cost_for_pair(prefix, in_tok, out_tok, rates)
        by_model.append({
            "model": prefix,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "input_cost_usd": _round6(in_cost),
            "output_cost_usd": _round6(out_cost),
            "total_cost_usd": _round6(sub_total),
            "total_cost_inr": _to_inr(sub_total, fx),
        })
        total += sub_total

    return {
        "project": project_label,
        "task_count": Task.search_count(date_domain),
        "total_cost_usd": _round6(total),
        "total_cost_inr": _to_inr(total, fx),
        "computed_from": "tokens × live pricing (ir.config_parameter)",
        "by_model": sorted(by_model, key=lambda r: -r["total_cost_usd"]),
    }


def _skoll_cost(env, date_domain, fx):
    Gen = env.get("skoll.generation")
    if Gen is None:
        return None
    Gen = Gen.sudo()

    by_model = []
    total = 0.0
    rows = Gen._read_group(
        date_domain,
        ["call_type"],
        ["input_tokens:sum", "output_tokens:sum", "total_cost:sum"],
    )
    for call_type, in_tok, out_tok, cost in rows:
        cost = float(cost or 0.0)
        in_tok = int(in_tok or 0)
        out_tok = int(out_tok or 0)
        if cost == 0.0 and in_tok == 0 and out_tok == 0:
            continue
        by_model.append({
            "model": call_type or "unknown",
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_cost_usd": _round6(cost),
            "total_cost_inr": _to_inr(cost, fx),
        })
        total += cost

    return {
        "project": "skoll",
        "task_count": Gen.search_count(date_domain),
        "total_cost_usd": _round6(total),
        "total_cost_inr": _to_inr(total, fx),
        "computed_from": "stored skoll.generation.total_cost",
        "by_model": sorted(by_model, key=lambda r: -r["total_cost_usd"]),
    }


def _kaiju_cost(env, date_domain, fx):
    Run = env.get("kaiju.commit0.run")
    if Run is None:
        return None
    Run = Run.sudo()

    by_model = []
    total = 0.0
    rows = Run._read_group(
        date_domain,
        ["model_name"],
        ["tokens_input:sum", "tokens_output:sum", "cost_usd:sum"],
    )
    for model_name, in_tok, out_tok, cost in rows:
        cost = float(cost or 0.0)
        in_tok = int(in_tok or 0)
        out_tok = int(out_tok or 0)
        if cost == 0.0 and in_tok == 0 and out_tok == 0:
            continue
        by_model.append({
            "model": model_name or "unknown",
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_cost_usd": _round6(cost),
            "total_cost_inr": _to_inr(cost, fx),
        })
        total += cost

    return {
        "project": "kaiju",
        "task_count": Run.search_count(date_domain),
        "total_cost_usd": _round6(total),
        "total_cost_inr": _to_inr(total, fx),
        "computed_from": "stored kaiju.commit0.run.cost_usd",
        "by_model": sorted(by_model, key=lambda r: -r["total_cost_usd"]),
    }


def _crowley_cost(env, date_domain, fx):
    Gen = env.get("crowley.generation")
    if Gen is None:
        return None
    Gen = Gen.sudo()

    by_model = []
    total = 0.0
    rows = Gen._read_group(
        date_domain,
        ["model_name"],
        ["tokens_used:sum", "cost_usd:sum"],
    )
    for model_name, tokens_used, cost in rows:
        cost = float(cost or 0.0)
        tokens_used = int(tokens_used or 0)
        if cost == 0.0 and tokens_used == 0:
            continue
        by_model.append({
            "model": model_name or "unknown",
            "tokens_used": tokens_used,
            "total_cost_usd": _round6(cost),
            "total_cost_inr": _to_inr(cost, fx),
        })
        total += cost

    return {
        "project": "crowley",
        "task_count": Gen.search_count(date_domain),
        "total_cost_usd": _round6(total),
        "total_cost_inr": _to_inr(total, fx),
        "computed_from": "stored crowley.generation.cost_usd",
        "by_model": sorted(by_model, key=lambda r: -r["total_cost_usd"]),
    }


# ─────────────────────────── drill-down per project ───────────────────────────


def _drill_kensei_or_talos(env, model_name, project_label, prefix, date_domain,
                            limit, offset, rates, fx):
    """List individual kensei/talos tasks whose chosen-prefix tokens > 0.

    `prefix` (e.g. 'claude', 'gpt', 'oneP') comes from the URL ?model= param.
    """
    Task = env.get(model_name)
    if Task is None:
        return None, return_Response(
            message=f"Model '{model_name}' not installed.", status=404,
        )
    Task = Task.sudo()

    in_field = f"{prefix}_input_tokens"
    out_field = f"{prefix}_output_tokens"
    if in_field not in Task._fields or out_field not in Task._fields:
        return None, return_Response(
            message=f"Project '{project_label}' has no '{prefix}' counter "
                    f"(expected fields {in_field}/{out_field}).",
            status=400,
        )

    domain = list(date_domain) + ["|",
                                  (in_field, ">", 0),
                                  (out_field, ">", 0)]
    total = Task.search_count(domain)
    records = Task.search(
        domain, limit=limit, offset=offset, order="create_date desc, id desc",
    )

    rows = []
    for rec in records:
        in_tok = getattr(rec, in_field) or 0
        out_tok = getattr(rec, out_field) or 0
        in_cost, out_cost, sub_total = cost_for_pair(prefix, in_tok, out_tok, rates)
        rows.append({
            "id": rec.id,
            "name": getattr(rec, "task_id", "") or "",
            "model": prefix,
            "input_tokens": int(in_tok),
            "output_tokens": int(out_tok),
            "input_cost_usd": _round6(in_cost),
            "output_cost_usd": _round6(out_cost),
            "total_cost_usd": _round6(sub_total),
            "total_cost_inr": _to_inr(sub_total, fx),
            "assigned_to_name":
                (rec.employee_id.name if "employee_id" in rec._fields and rec.employee_id else ""),
            "created_at": _iso(rec.create_date),
            "updated_at": _iso(rec.write_date),
        })
    return {
        "project": project_label,
        "model": prefix,
        "computed_from": "tokens × live pricing (ir.config_parameter)",
        "total_records": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }, None


def _drill_skoll(env, model, date_domain, limit, offset, fx):
    Gen = env.get("skoll.generation")
    if Gen is None:
        return None, return_Response(
            message="Model 'skoll.generation' not installed.", status=404,
        )
    Gen = Gen.sudo()

    valid_call_types = dict(Gen._fields["call_type"].selection or [])
    if model not in valid_call_types:
        return None, return_Response(
            message=f"Invalid model '{model}' for skoll. "
                    f"Allowed: {', '.join(sorted(valid_call_types))}.",
            status=400,
        )

    domain = list(date_domain) + [("call_type", "=", model)]
    total = Gen.search_count(domain)
    records = Gen.search(
        domain, limit=limit, offset=offset, order="create_date desc, id desc",
    )
    rows = [{
        "id": r.id,
        "name": r.task_ref or (r.task_id.task_id if r.task_id else "") or "",
        "model": r.call_type or "",
        "input_tokens": int(r.input_tokens or 0),
        "output_tokens": int(r.output_tokens or 0),
        "total_cost_usd": _round6(r.total_cost),
        "total_cost_inr": _to_inr(r.total_cost, fx),
        "assigned_to_name": r.user_id.name if r.user_id else "",
        "duration_s": float(r.duration_s or 0.0),
        "status": r.status or "",
        "created_at": _iso(r.create_date),
        "updated_at": _iso(r.write_date),
    } for r in records]
    return {
        "project": "skoll",
        "model": model,
        "computed_from": "stored skoll.generation.total_cost",
        "total_records": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }, None


def _drill_kaiju(env, model, date_domain, limit, offset, fx):
    Run = env.get("kaiju.commit0.run")
    if Run is None:
        return None, return_Response(
            message="Model 'kaiju.commit0.run' not installed.", status=404,
        )
    Run = Run.sudo()

    domain = list(date_domain) + [("model_name", "=", model)]
    total = Run.search_count(domain)
    records = Run.search(
        domain, limit=limit, offset=offset, order="create_date desc, id desc",
    )
    rows = [{
        "id": r.id,
        "name": r.name or "",
        "model": r.model_name or "",
        "input_tokens": int(r.tokens_input or 0),
        "output_tokens": int(r.tokens_output or 0),
        "total_cost_usd": _round6(r.cost_usd),
        "total_cost_inr": _to_inr(r.cost_usd, fx),
        "run_status": r.run_status or "",
        "duration_s": float(r.duration_seconds or 0.0),
        "pass_rate": float(r.pass_rate or 0.0),
        "created_at": _iso(r.create_date),
        "updated_at": _iso(r.write_date),
    } for r in records]
    return {
        "project": "kaiju",
        "model": model,
        "computed_from": "stored kaiju.commit0.run.cost_usd",
        "total_records": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }, None


def _drill_crowley(env, model, date_domain, limit, offset, fx):
    Gen = env.get("crowley.generation")
    if Gen is None:
        return None, return_Response(
            message="Model 'crowley.generation' not installed.", status=404,
        )
    Gen = Gen.sudo()

    domain = list(date_domain) + [("model_name", "=", model)]
    total = Gen.search_count(domain)
    records = Gen.search(
        domain, limit=limit, offset=offset, order="create_date desc, id desc",
    )
    rows = [{
        "id": r.id,
        "name": r.name or "",
        "model": r.model_name or "",
        "tokens_used": int(r.tokens_used or 0),
        "total_cost_usd": _round6(r.cost_usd),
        "total_cost_inr": _to_inr(r.cost_usd, fx),
        "cost_usd_estimate": _round6(r.cost_usd_estimate),
        "retry_count": int(r.retry_count or 0),
        "review_state": r.review_state or "",
        "created_at": _iso(r.create_date),
        "updated_at": _iso(r.write_date),
    } for r in records]
    return {
        "project": "crowley",
        "model": model,
        "computed_from": "stored crowley.generation.cost_usd",
        "total_records": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }, None


# ─────────────────────────── controller ───────────────────────────


class CrowleyExtensionLLMCostDashboard(http.Controller):

    @http.route(
        "/api/v1/crowley_extension/llm_cost_summary",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def llm_cost_summary(self, **kwargs):
        env = request.env

        guard = _require_cto_or_tpm(env)
        if guard is not None:
            return guard

        date_domain, err = _parse_date_filters(request.params or {})
        if err is not None:
            return err

        rates = get_pricing(env)
        fx = get_fx_usd_inr(env)
        meta = get_last_updated(env)

        projects = []
        grand_total = 0.0

        sources = [
            ("kensei.kensei", "kensei", _kensei_or_talos_cost),
            ("talos.talos",   "talos",  _kensei_or_talos_cost),
            (None,            "skoll",   _skoll_cost),
            (None,            "kaiju",   _kaiju_cost),
            (None,            "crowley", _crowley_cost),
        ]

        for model_name, label, fn in sources:
            try:
                if model_name is not None:
                    data = fn(env, model_name, label, date_domain, rates, fx)
                else:
                    data = fn(env, date_domain, fx)
            except Exception as exc:
                data = {
                    "project": label,
                    "task_count": 0,
                    "total_cost_usd": 0.0,
                    "total_cost_inr": _to_inr(0.0, fx),
                    "computed_from": "error",
                    "error": str(exc),
                    "by_model": [],
                }
            if data is None:
                continue
            projects.append(data)
            grand_total += data["total_cost_usd"]

        projects.sort(key=lambda p: -p["total_cost_usd"])

        data = {
            "total_cost_usd": _round6(grand_total),
            "total_cost_inr": _to_inr(grand_total, fx),
            "project_count": len(projects),
            "filters": {
                "start_date": (request.params or {}).get("start_date") or "",
                "end_date": (request.params or {}).get("end_date") or "",
            },
            "pricing": {
                "fx_usd_inr": fx,
                "pricing_last_updated": meta["pricing_last_updated"],
                "fx_last_updated": meta["fx_last_updated"],
            },
            "projects": projects,
        }
        return return_Response(message="OK", status=200, data=data)

    @http.route(
        "/api/v1/crowley_extension/llm_cost_breakdown",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def llm_cost_breakdown(self, **kwargs):
        env = request.env

        guard = _require_cto_or_tpm(env)
        if guard is not None:
            return guard

        params = request.params or {}

        project = (params.get("project") or "").strip().lower()
        if project not in VALID_PROJECTS:
            return return_Response(
                message=f"Invalid or missing project '{project}'. "
                        f"Allowed: {', '.join(VALID_PROJECTS)}.",
                status=400,
            )

        model = (params.get("model") or "").strip()
        if not model:
            return return_Response(
                message="model is required (e.g. claude, gpt, oneP, "
                        "generate, qc, improve, bytedance/seedance-2.0).",
                status=400,
            )

        date_domain, err = _parse_date_filters(params)
        if err is not None:
            return err

        limit = _coerce_int(params.get("limit"), BREAKDOWN_DEFAULT_LIMIT) \
            or BREAKDOWN_DEFAULT_LIMIT
        if limit > BREAKDOWN_MAX_LIMIT:
            limit = BREAKDOWN_MAX_LIMIT
        offset = _coerce_int(params.get("offset"), 0)

        rates = get_pricing(env)
        fx = get_fx_usd_inr(env)
        meta = get_last_updated(env)

        if project == "kensei":
            data, err = _drill_kensei_or_talos(
                env, "kensei.kensei", "kensei", model,
                date_domain, limit, offset, rates, fx,
            )
        elif project == "talos":
            data, err = _drill_kensei_or_talos(
                env, "talos.talos", "talos", model,
                date_domain, limit, offset, rates, fx,
            )
        elif project == "skoll":
            data, err = _drill_skoll(env, model, date_domain, limit, offset, fx)
        elif project == "kaiju":
            data, err = _drill_kaiju(env, model, date_domain, limit, offset, fx)
        elif project == "crowley":
            data, err = _drill_crowley(env, model, date_domain, limit, offset, fx)
        else:
            return return_Response(message="Unreachable.", status=500)

        if err is not None:
            return err

        data["filters"] = {
            "start_date": params.get("start_date") or "",
            "end_date": params.get("end_date") or "",
        }
        data["pricing"] = {
            "fx_usd_inr": fx,
            "pricing_last_updated": meta["pricing_last_updated"],
            "fx_last_updated": meta["fx_last_updated"],
        }
        return return_Response(message="OK", status=200, data=data)
