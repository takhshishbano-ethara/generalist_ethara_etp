"""Aurora analytics tab.

Sections Aurora has a source for are filled with real data (kpi,
tasks_submitted_per_day, qc_verdict_mix → resolution-outcome mix); sections
that depend on cost / QL / review data Aurora doesn't record
(spend_by_category, qc_pass_rate_by_ql, daily_burn_rate, qc_verdicts_per_day)
are returned blank ({}). Computed live from aurora.evaluation.instance.
"""

import logging
from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

from .common import (
    COMPLETED_STATES,
    kpi_item,
    pct,
    range_domain,
    range_label,
    resolve_range,
    user_role_tag,
)

VERDICT_BUCKETS = (
    ("resolved", "Resolved", "success"),
    ("unresolved", "Unresolved", "danger"),
    ("error", "Error", "warn"),
)


def _build_kpi(env, scope, rng):
    Instance = env["aurora.evaluation.instance"].sudo()
    total = Instance.search_count(scope)
    resolved = Instance.search_count(scope + [("status", "in", list(COMPLETED_STATES))])
    unresolved = Instance.search_count(scope + [("status", "=", "unresolved")])
    errored = Instance.search_count(scope + [("status", "=", "error")])
    items = [
        kpi_item(
            "resolve_rate", "Resolve Rate", f"{pct(resolved, total)}%",
            sub_string=f"{resolved} of {total} instances",
        ),
        kpi_item(
            "total_instances", "Total Instances", total,
            sub_string=range_label(rng),
        ),
        kpi_item(
            "resolved", "Resolved", resolved,
            sub_string=f"{unresolved} unresolved",
        ),
        kpi_item(
            "errored", "Errored", errored,
            sub_string="instances in error",
        ),
    ]
    return {"count": len(items), "items": items}


def _per_day_series(env, scope, rng):
    # TODO(perf): if this date window ever spans a large volume, switch to
    # _read_group(create_date:day) — but preserve UTC-day bucketing (this loop
    # buckets by rec.create_date.date(), i.e. UTC, not the user tz) so the
    # daily_burn_rate chart values stay identical.
    Instance = env["aurora.evaluation.instance"].sudo()
    per_day = {}
    for rec in Instance.search(scope):
        if rec.create_date:
            day = rec.create_date.date()
            per_day[day] = per_day.get(day, 0) + 1
    data = []
    total = 0
    cursor = rng["start"]
    while cursor <= rng["end"]:
        count = per_day.get(cursor, 0)
        total += count
        data.append({
            "date": cursor.isoformat(),
            "label": cursor.strftime("%b %d"),
            "value": count,
        })
        cursor += timedelta(days=1)
    return data, total


def _build_per_day_burn(env, scope, rng):
    """Per-day instance volume in crowley's `daily_burn_rate` shape — the only
    per-day chart key the InternalAnalyticsTab actually renders. Each point is
    {date, total, segments:[...]} with float values; one segment = a simple
    per-day bar."""
    data, total = _per_day_series(env, scope, rng)
    points = [
        {
            "date": d["date"],
            "total": float(d["value"]),
            "segments": [float(d["value"])],
        }
        for d in data
    ]
    return {
        "title": "Instances Created per Day",
        "sub_title": range_label(rng),
        "type": "bar",
        "headline": str(total),
        "headline_caption": "instances in range",
        "legend": [{"label": "Instances", "color": "primary"}],
        "data": points,
    }


def _build_verdict_mix(env, scope, rng):
    """Resolution-outcome mix — Aurora's analog to crowley's QC verdict mix."""
    Instance = env["aurora.evaluation.instance"].sudo()
    counts = {
        key: Instance.search_count(scope + [("status", "=", key)])
        for key, _label, _color in VERDICT_BUCKETS
    }
    total = sum(counts.values())
    items = []
    for key, label, color in VERDICT_BUCKETS:
        amount = counts[key]
        percentage = pct(amount, total)
        items.append({
            "key": key,
            "label": label,
            "value": f"{amount} ({percentage:.0f}%)",
            "amount": amount,
            "percentage": percentage,
            "color_token": color,
        })
    return {
        "title": "Resolution Mix",
        "sub_title": range_label(rng),
        "type": "stacked_bar",
        "total": total,
        "items": items,
    }


class AuroraAnalyticsController(http.Controller):

    @http.route(
        "/api/v1/aurora_ext/analytics_dashboard",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    @validate_token
    def aurora_ext_analytics_dashboard(self, **kwargs):
        env = request.env
        role_tag = user_role_tag(env)
        if role_tag is None:
            return return_Response(
                message="You are not allowed to access Aurora data.",
                status=403,
            )

        rng, error = resolve_range(request.params or {})
        if error is not None:
            return error
        scope = range_domain(rng)

        # Every analytics section key is always present (crowley_sourcing
        # parity). Cost / QL / review sections Aurora has no source for are
        # returned blank ({}).
        try:
            data = {
                "role": role_tag,
                "kpi": _build_kpi(env, scope, rng),
                # The InternalAnalyticsTab only renders kpi / spend_by_category /
                # qc_pass_rate_by_ql / daily_burn_rate. So Aurora's charts live
                # under those keys: spend_by_category = resolution mix,
                # daily_burn_rate = instances/day. qc_pass_rate_by_ql stays empty
                # (no QC data). The remaining crowley keys are kept (empty) for
                # schema parity but the UI ignores them.
                "spend_by_category": _build_verdict_mix(env, scope, rng),
                "qc_pass_rate_by_ql": {},
                "daily_burn_rate": _build_per_day_burn(env, scope, rng),
                "tasks_submitted_per_day": {},
                "qc_verdict_mix": {},
                "qc_verdicts_per_day": {},
            }
        except Exception:
            _logger.exception("aurora_ext_analytics_dashboard failed")
            return return_Response(
                message="Failed to build Aurora analytics.",
                status=400,
            )

        return return_Response(message="OK", status=200, data=data)
