"""Ethara Project budget health dashboard data provider.

Hosts the aggregation logic for the 8 budget-dashboard HTTP endpoints
(kpis, actual_vs_estimated, spend_by_model, phase_budget, burn_rate,
project_table, project_performance_list, phase_health).

Ported from ``etp_projects/controllers/project_budget_dashboard.py`` with the
following model / field renames applied:

  * ``etp.project.aws.budget``      -> ``ethara.project.budget``
  * ``etp.batch.budget``            -> ``ethara.project.phase``
  * ``etp.project.aws.cost.line``   -> ``ethara.project.cost.line``
  * ``etp.project.budget.topup``    -> ``ethara.project.budget.topup``
  * ``project_id``                  -> ``ethara_project_id``
  * ``batch_id``                    -> ``phase_id``
  * ``project_budget_id``           -> ``budget_id``
  * ``budget_type``                 -> ``project_type``
  * ``batch_budget``                -> ``phase_budget``

Fields with no ethara equivalent default to a sensible zero/empty value
rather than raising:

  * project production-state count / active-project heuristics: the etp KPI
    endpoint used ``project.project`` non-stemp status; the ethara KPI payload
    below is the *new* (``controllers/project_budget_dashboard.py``) shape,
    which never needed those, so nothing is dropped.
  * AI model ``provider`` -> ``ethara.project.ai.model.provider`` (exists).
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta

from odoo import fields, models
from odoo.exceptions import ValidationError

BUDGET_MODEL = "ethara.project.budget"
PHASE_MODEL = "ethara.project.phase"
COST_LINE_MODEL = "ethara.project.cost.line"
TOPUP_MODEL = "ethara.project.budget.topup"

NO_MODEL_LABEL = "(no model)"

MODEL_BREAKDOWN_SERVICES = ["Amazon Bedrock", "OpenRouter"]

# Phase states considered "active" for rollups (draft/rejected/withdrawn out).
ACTIVE_PHASE_STATES = ("approved", "in_progress", "delivered", "closed")
APPROVED_TOPUP_STATE = "approved"
TOTAL_APPROVED_EXCLUDED_PHASE_STATES = ("rejected", "withdrawn")

HEALTH_HEALTHY_PCT = 60.0
HEALTH_WARNING_PCT = 80.0
HEALTH_AT_RISK_PCT = 100.0

ACCURACY_GOOD_PCT = 110.0
UNHEALTHY_HEALTHS = ("warning", "at_risk", "critical")

BURN_VIEWS = ("daily", "weekly", "monthly")
DEFAULT_BURN_VIEW = "daily"
BUCKET_UNITS = {"daily": "4h", "weekly": "day", "monthly": "week"}
DAILY_HOUR_SLOTS = (0, 4, 8, 12, 16, 20)


def _round2(value):
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _safe_pct(part, whole):
    if not whole:
        return 0.0
    return _round2((float(part) / float(whole)) * 100.0)


def _coerce_date(value, field_name):
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
    raise ValidationError("'%s' must be a date in YYYY-MM-DD format." % field_name)


def _health_from_pct(pct, has_envelope):
    if not has_envelope:
        return "unknown"
    if pct < HEALTH_HEALTHY_PCT:
        return "healthy"
    if pct < HEALTH_WARNING_PCT:
        return "warning"
    if pct < HEALTH_AT_RISK_PCT:
        return "at_risk"
    return "critical"


def _resolve_window(jdata):
    start = _coerce_date(jdata.get("start"), "start")
    end = _coerce_date(jdata.get("end"), "end")
    if start and end:
        if start > end:
            raise ValidationError("'start' must be on or before 'end'.")
        return start, end, "date_range"
    if start or end:
        raise ValidationError("Provide both 'start' and 'end' for a date range.")

    month_str = jdata.get("month")
    if month_str:
        if not isinstance(month_str, str):
            raise ValidationError("'month' must be a string in 'YYYY-MM' format.")
        try:
            mdate = datetime.strptime(month_str, "%Y-%m").date()
        except ValueError:
            raise ValidationError("'month' must be a string in 'YYYY-MM' format.")
    else:
        mdate = date.today()
    m_start = mdate.replace(day=1)
    m_end = (m_start + relativedelta(months=1)) - timedelta(days=1)
    return m_start, m_end, "month"


def _burn_resolve_window(jdata, view):
    start = _coerce_date(jdata.get("start"), "start")
    end = _coerce_date(jdata.get("end"), "end")
    if start and end:
        if start > end:
            raise ValidationError("'start' must be on or before 'end'.")
        return start, end, "date_range"
    if start or end:
        raise ValidationError("Provide both 'start' and 'end' for a date range.")

    month_str = jdata.get("month")
    if month_str:
        if not isinstance(month_str, str):
            raise ValidationError("'month' must be a string in 'YYYY-MM' format.")
        try:
            mdate = datetime.strptime(month_str, "%Y-%m").date()
        except ValueError:
            raise ValidationError("'month' must be a string in 'YYYY-MM' format.")
        m_start = mdate.replace(day=1)
        m_end = (m_start + relativedelta(months=1)) - timedelta(days=1)
        return m_start, m_end, "month"

    today = date.today()
    if view == "weekly":
        return today - timedelta(days=6), today, "view_default"
    if view == "monthly":
        m_start = today.replace(day=1)
        m_end = (m_start + relativedelta(months=1)) - timedelta(days=1)
        return m_start, m_end, "view_default"
    return today, today, "view_default"


def _previous_window(start, end):
    return start - relativedelta(months=1), end - relativedelta(months=1)


def _enumerate_burn_buckets(start, end, view):
    buckets = []
    if view == "daily":
        d = start
        while d <= end:
            for h in DAILY_HOUR_SLOTS:
                key = f"{d.isoformat()}T{h:02d}"
                label = f"{d.isoformat()} {h:02d}:00-{(h + 4):02d}:00"
                buckets.append({
                    "key": key,
                    "label": label,
                    "bucket_start": f"{d.isoformat()}T{h:02d}:00:00",
                    "bucket_end": f"{d.isoformat()}T{(h + 4) % 24:02d}:00:00",
                })
            d += timedelta(days=1)
        return buckets
    if view == "weekly":
        d = start
        while d <= end:
            key = d.isoformat()
            buckets.append({
                "key": key,
                "label": key,
                "bucket_start": key,
                "bucket_end": key,
            })
            d += timedelta(days=1)
        return buckets
    d = start
    seen = set()
    while d <= end:
        iy, iw, idow = d.isocalendar()
        key = f"{iy}-W{iw:02d}"
        if key not in seen:
            wk_start = d - timedelta(days=idow - 1)
            wk_end = wk_start + timedelta(days=6)
            buckets.append({
                "key": key,
                "label": f"{key} ({wk_start.isoformat()}..{wk_end.isoformat()})",
                "bucket_start": wk_start.isoformat(),
                "bucket_end": wk_end.isoformat(),
            })
            seen.add(key)
        d += timedelta(days=1)
    return buckets


def _bucket_assignments(period, view):
    if view == "daily":
        weight = 1.0 / len(DAILY_HOUR_SLOTS)
        return [(f"{period.isoformat()}T{h:02d}", weight) for h in DAILY_HOUR_SLOTS]
    if view == "weekly":
        return [(period.isoformat(), 1.0)]
    iy, iw, _ = period.isocalendar()
    return [(f"{iy}-W{iw:02d}", 1.0)]


class EtharaProjectBudgetDashboard(models.AbstractModel):
    _name = "ethara.project.budget.dashboard"
    _description = "Ethara Project Budget Health Dashboard Data Provider"

    # ------------------------------------------------------------------
    # Legacy simple provider (kept for any existing internal callers).
    # ------------------------------------------------------------------
    def get_dashboard_data(self, project_id=None):
        Budget = self.env["ethara.project.budget"].sudo()
        domain = [("active", "=", True)]
        if project_id:
            domain.append(("ethara_project_id", "=", int(project_id)))
        budgets = Budget.search(domain)

        rows = []
        agg_total_env = 0.0
        agg_consumed = 0.0
        agg_remaining = 0.0
        health_buckets = {
            "unknown": 0,
            "healthy": 0,
            "warning": 0,
            "at_risk": 0,
            "critical": 0,
        }
        for b in budgets:
            rows.append({
                "id": b.id,
                "name": b.name or "",
                "project_id": b.ethara_project_id.id,
                "project_name": b.ethara_project_id.name or "",
                "state": b.state,
                "project_type": b.project_type,
                "budget_amount": float(b.budget_amount or 0.0),
                "consumed_amount": float(b.consumed_amount or 0.0),
                "remaining_amount": float(b.remaining_amount or 0.0),
                "consumed_pct": float(b.consumed_pct or 0.0),
                "health_status": b.health_status or "unknown",
                "last_fetched_at": (
                    b.last_fetched_at.isoformat() if b.last_fetched_at else None
                ),
            })
            agg_total_env += float(b.budget_amount or 0.0)
            agg_consumed += float(b.consumed_amount or 0.0)
            agg_remaining += float(b.remaining_amount or 0.0)
            key = b.health_status if b.health_status in health_buckets else "unknown"
            health_buckets[key] = health_buckets.get(key, 0) + 1

        return {
            "totals": {
                "budget_amount": agg_total_env,
                "consumed_amount": agg_consumed,
                "remaining_amount": agg_remaining,
                "consumed_pct": (
                    (agg_consumed / agg_total_env * 100.0)
                    if agg_total_env else 0.0
                ),
                "budget_count": len(rows),
            },
            "health_counts": health_buckets,
            "budgets": rows,
        }

    # ==================================================================
    # Shared aggregation helpers (env-bound).
    # ==================================================================
    def _portfolio_currency(self, budgets=None):
        usd = self.env.ref("base.USD", raise_if_not_found=False)
        if usd:
            return {"currency": usd.name or "USD", "currency_symbol": usd.symbol or "$"}
        return {"currency": "USD", "currency_symbol": "$"}

    def _spend_by_budget(self, budget_ids, start, end):
        if not budget_ids:
            return {}
        groups = self.env[COST_LINE_MODEL].sudo()._read_group(
            [
                ("budget_id", "in", list(budget_ids)),
                ("granularity", "=", "day"),
                ("is_model_breakdown", "=", False),
                ("period", ">=", start),
                ("period", "<=", end),
            ],
            groupby=["budget_id"],
            aggregates=["amount_source:sum"],
        )
        return {b.id: float(a or 0.0) for (b, a) in groups}

    def _model_spend_by_budget(self, budget_ids, start, end):
        out = defaultdict(lambda: defaultdict(float))
        if not budget_ids:
            return out
        Lines = self.env[COST_LINE_MODEL].sudo()
        base_domain = [
            ("budget_id", "in", list(budget_ids)),
            ("granularity", "=", "day"),
            ("period", ">=", start),
            ("period", "<=", end),
        ]
        bedrock_groups = Lines._read_group(
            base_domain + [
                ("service_name", "in", MODEL_BREAKDOWN_SERVICES),
                ("is_model_breakdown", "=", True),
            ],
            groupby=["budget_id", "model_name"],
            aggregates=["amount_source:sum"],
        )
        for (b, mn, amt) in bedrock_groups:
            out[b.id][mn or NO_MODEL_LABEL] += float(amt or 0.0)
        other_groups = Lines._read_group(
            base_domain + [
                ("service_name", "not in", MODEL_BREAKDOWN_SERVICES),
                ("is_model_breakdown", "=", False),
            ],
            groupby=["budget_id", "service_name"],
            aggregates=["amount_source:sum"],
        )
        for (b, sn, amt) in other_groups:
            out[b.id][sn or NO_MODEL_LABEL] += float(amt or 0.0)
        return out

    def _effective_model_spend_by_budget(self, budget_ids, start, end):
        out = defaultdict(lambda: defaultdict(float))
        if not budget_ids:
            return out
        Lines = self.env[COST_LINE_MODEL].sudo()
        base_domain = [
            ("budget_id", "in", list(budget_ids)),
            ("granularity", "=", "day"),
            ("period", ">=", start),
            ("period", "<=", end),
        ]
        breakdown_groups = Lines._read_group(
            base_domain + [("is_model_breakdown", "=", True)],
            groupby=["budget_id", "model_name"],
            aggregates=["amount_source:sum"],
        )
        breakdown_services = set()
        for (b, mn, amt) in breakdown_groups:
            key = mn or NO_MODEL_LABEL
            out[b.id][key] += float(amt or 0.0)
        bd_service_rows = Lines._read_group(
            base_domain + [("is_model_breakdown", "=", True)],
            groupby=["budget_id", "service_name"],
            aggregates=["amount_source:sum"],
        )
        for (b, sn, _amt) in bd_service_rows:
            breakdown_services.add((b.id, sn or ""))
        service_groups = Lines._read_group(
            base_domain + [("is_model_breakdown", "=", False)],
            groupby=["budget_id", "service_name", "model_name"],
            aggregates=["amount_source:sum"],
        )
        for (b, sn, mn, amt) in service_groups:
            if (b.id, sn or "") in breakdown_services:
                continue
            key = mn or sn or NO_MODEL_LABEL
            out[b.id][key] += float(amt or 0.0)
        return out

    def _estimated_by_model_for_budgets(self, budget_ids, start, end):
        out = defaultdict(lambda: defaultdict(float))
        if not budget_ids:
            return out
        domain = [
            ("budget_id", "in", list(budget_ids)),
            ("state", "in", list(ACTIVE_PHASE_STATES)),
        ]
        if start and end:
            domain += [
                ("start_date", "<=", end),
                ("end_date", ">=", start),
            ]
        phases = self.env[PHASE_MODEL].sudo().search(domain)
        for px in phases:
            bid = px.budget_id.id
            total_tasks = float(px.total_tasks or 0)
            for ml in px.model_line_ids:
                if not ml.ai_model_id:
                    continue
                name = ml.ai_model_id.name or NO_MODEL_LABEL
                out[bid][name] += total_tasks * float(ml.per_task_cost or 0.0)
        return out

    def _topups_by_budget(self, budget_ids):
        if not budget_ids:
            return {}
        groups = self.env[TOPUP_MODEL].sudo()._read_group(
            [
                ("budget_id", "in", list(budget_ids)),
                ("state", "=", APPROVED_TOPUP_STATE),
            ],
            groupby=["budget_id"],
            aggregates=["amount:sum"],
        )
        return {b.id: float(a or 0.0) for (b, a) in groups}

    def _batches_by_budget(self, budget_ids, start, end):
        out = defaultdict(lambda: {"estimated": 0.0, "approved": 0.0})
        if not budget_ids:
            return out
        domain = [
            ("budget_id", "in", list(budget_ids)),
            ("state", "in", list(ACTIVE_PHASE_STATES)),
        ]
        if start and end:
            domain += [
                ("start_date", "<=", end),
                ("end_date", ">=", start),
            ]
        phases = self.env[PHASE_MODEL].sudo().search(domain)
        for px in phases:
            bucket = out[px.budget_id.id]
            bucket["estimated"] += float(px.estimated_cost or 0.0)
            bucket["approved"] += float(px.approved_amount or 0.0)
        return out

    def _total_approved_by_budget(self, budgets):
        out = {}
        if not budgets:
            return out
        bids = [b.id for b in budgets]
        approved_by_id = defaultdict(float)
        phases = self.env[PHASE_MODEL].sudo().search([
            ("budget_id", "in", bids),
            ("state", "not in", list(TOTAL_APPROVED_EXCLUDED_PHASE_STATES)),
        ])
        for px in phases:
            approved_by_id[px.budget_id.id] += float(px.approved_amount or 0.0)
        for bid in bids:
            out[bid] = float(approved_by_id.get(bid, 0.0))
        return out

    def _phase_budget_chart(self, budget_ids, start=None, end=None):
        if not budget_ids:
            return []
        domain = [
            ("budget_id", "in", list(budget_ids)),
            ("state", "in", list(ACTIVE_PHASE_STATES)),
        ]
        if start and end:
            domain += [
                ("start_date", "<=", end),
                ("end_date", ">=", start),
            ]
        phases = self.env[PHASE_MODEL].sudo().search(domain)
        rows = []
        for px in phases:
            budget_amt = float(px.approved_amount or 0.0)
            estimated = float(px.estimated_cost or 0.0)
            actual = float(px.consumed_cost or 0.0)
            rows.append({
                "batch_id": px.id,
                "phase_name": px.name,
                "batch_name": px.name,
                "project_id": (
                    px.ethara_project_id.id if px.ethara_project_id else False
                ),
                "project_name": (
                    px.ethara_project_id.display_name
                    if px.ethara_project_id else ""
                ),
                "budget": _round2(budget_amt),
                "estimated": _round2(estimated),
                "actual": _round2(actual),
                "delta_actual_vs_estimated": _round2(actual - estimated),
                "delta_actual_vs_budget": _round2(actual - budget_amt),
                "estimated_vs_budget_pct": (
                    _round2((estimated / budget_amt) * 100.0) if budget_amt else 0.0
                ),
                "actual_vs_budget_pct": (
                    _round2((actual / budget_amt) * 100.0) if budget_amt else 0.0
                ),
                "actual_vs_estimated_pct": (
                    _round2((actual / estimated) * 100.0) if estimated else 0.0
                ),
            })
        rows.sort(key=lambda r: r["actual"], reverse=True)
        return rows

    def _project_row(self, budget, spend, topups, batches, days,
                     models_cur, models_prev, total_approved):
        is_rnd = (budget.project_type == "rnd")
        budget_amount = float(budget.budget_amount or 0.0)
        topup_total = float(topups.get(budget.id, 0.0))
        final_budget = float(total_approved.get(budget.id, 0.0))
        actual = float(spend.get(budget.id, 0.0))

        bucket = batches.get(budget.id) or {"estimated": 0.0, "approved": 0.0}
        expected = float(bucket["estimated"])
        approved_total = float(bucket["approved"])

        consumed_pct = (actual / final_budget * 100.0) if final_budget else 0.0
        health = _health_from_pct(consumed_pct, final_budget > 0)
        health_score = (
            _round2(max(0.0, 100.0 - consumed_pct)) if final_budget else None
        )

        if expected <= 0:
            accuracy_score = None
            actual_vs_estimated_pct = None
        else:
            accuracy_score = _round2((actual / expected) * 100.0)
            actual_vs_estimated_pct = accuracy_score

        pct_over_original = (
            _round2(((actual - final_budget) / final_budget) * 100.0)
            if final_budget else 0.0
        )

        over_budget = bool(final_budget and actual > final_budget)

        risk_amount = max(0.0, actual - approved_total)
        amount_at_risk_pct = (
            _round2((risk_amount / final_budget) * 100.0) if final_budget else 0.0
        )

        run_per_day = (actual / days) if days else 0.0
        run_pct_per_day = (
            _round2((run_per_day / final_budget) * 100.0) if final_budget else 0.0
        )

        current_models = models_cur.get(budget.id, {})
        previous_models = models_prev.get(budget.id, {})
        model_keys = sorted(set(current_models) | set(previous_models))
        model_rows = []
        for mn in model_keys:
            cur = float(current_models.get(mn, 0.0))
            prev = float(previous_models.get(mn, 0.0))
            change = (
                ((cur - prev) / prev * 100.0) if prev
                else (100.0 if cur else 0.0)
            )
            share = (cur / actual * 100.0) if actual else 0.0
            model_rows.append({
                "model_name": mn,
                "current": _round2(cur),
                "last_month": _round2(prev),
                "change_pct_vs_last_month": _round2(change),
                "share_pct_current": _round2(share),
            })
        model_rows.sort(key=lambda r: r["current"], reverse=True)

        return {
            "project_id": (
                budget.ethara_project_id.id if budget.ethara_project_id else False
            ),
            "project_name": (
                budget.ethara_project_id.display_name
                if budget.ethara_project_id else ""
            ),
            "budget_id": budget.id,
            "budget_name": budget.name,
            "budget_type": budget.project_type or False,
            "is_rnd": is_rnd,
            "budget_amount": _round2(budget_amount),
            "topup_total": _round2(topup_total),
            "final_budget": _round2(final_budget),
            "approved_total": _round2(approved_total),
            "expected_cost": _round2(expected),
            "total_spend_llm": _round2(actual),
            "actual_spend": _round2(actual),
            "remaining": _round2(final_budget - actual),
            "consumed_pct": _round2(consumed_pct),
            "amount_at_risk_pct": amount_at_risk_pct,
            "run_per_day": _round2(run_per_day),
            "run_pct_per_day": run_pct_per_day,
            "health": health,
            "health_score": health_score,
            "accuracy_score": accuracy_score,
            "actual_vs_estimated_pct": actual_vs_estimated_pct,
            "pct_over_original_budget": pct_over_original,
            "over_budget": over_budget,
            "model_consumption": model_rows,
        }

    def _actual_vs_estimated_chart(self, rows):
        chart = []
        for r in rows:
            estimated = r["expected_cost"] or 0.0
            actual = r["actual_spend"]
            chart.append({
                "project_id": r["project_id"],
                "project_name": r["project_name"],
                "budget_id": r["budget_id"],
                "budget_name": r["budget_name"],
                "actual": actual,
                "estimated": _round2(estimated),
                "delta": _round2(actual - estimated),
                "actual_vs_estimated_pct": (
                    _round2((actual / estimated) * 100.0) if estimated > 0 else 0.0
                ),
            })
        chart.sort(key=lambda r: r["actual"], reverse=True)
        return chart

    def _spend_by_model_chart(self, models_cur):
        by_model = defaultdict(float)
        total = 0.0
        for bid_models in models_cur.values():
            for mn, amt in bid_models.items():
                by_model[mn] += float(amt or 0.0)
                total += float(amt or 0.0)
        rows = []
        for mn in sorted(by_model, key=lambda k: by_model[k], reverse=True):
            amt = by_model[mn]
            rows.append({
                "model_name": mn,
                "spend": _round2(amt),
                "share_pct": _safe_pct(amt, total),
            })
        return {"rows": rows, "total_spend": _round2(total)}

    # ==================================================================
    # Endpoint compute methods (called by the HTTP controllers).
    # ==================================================================
    def _dash_kpis(self, jdata):
        start, end, filter_type = _resolve_window(jdata)
        days = max(1, (end - start).days + 1)
        prev_start, prev_end = _previous_window(start, end)

        budgets = self.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
        bids = budgets.ids

        spend = self._spend_by_budget(bids, start, end)
        topups = self._topups_by_budget(bids)
        batches = self._batches_by_budget(bids, start, end)
        total_approved = self._total_approved_by_budget(budgets)
        models_cur = self._model_spend_by_budget(bids, start, end)
        models_prev = self._model_spend_by_budget(bids, prev_start, prev_end)

        rows = [
            self._project_row(
                b, spend, topups, batches, days,
                models_cur, models_prev, total_approved,
            )
            for b in budgets
        ]
        rows.sort(key=lambda r: r["consumed_pct"], reverse=True)

        project_ids = {r["project_id"] for r in rows if r["project_id"]}
        project_count = len(project_ids)

        total_spend_llm = sum(r["actual_spend"] for r in rows)
        total_budget = sum(r["budget_amount"] for r in rows)
        total_topups = sum(r["topup_total"] for r in rows)
        total_final_budget = sum(r["final_budget"] for r in rows)

        expected_total = sum((r["expected_cost"] or 0.0) for r in rows)
        approved_total = sum(r["approved_total"] for r in rows)

        actual_vs_estimated_pct = (
            _round2((total_spend_llm / expected_total) * 100.0)
            if expected_total > 0 else 0.0
        )
        pct_over_original_budget = (
            _round2(((total_spend_llm - total_final_budget) / total_final_budget) * 100.0)
            if total_final_budget else 0.0
        )

        over_budget_rows = [r for r in rows if r["over_budget"]]
        over_budget_project_count = len({
            r["project_id"] for r in over_budget_rows if r["project_id"]
        })

        accurate_rows = [
            r for r in rows
            if r["accuracy_score"] is not None
            and r["accuracy_score"] <= ACCURACY_GOOD_PCT
        ]
        estimation_accuracy_count = len(accurate_rows)
        estimation_accuracy_score = (
            _round2((total_spend_llm / expected_total) * 100.0)
            if expected_total > 0 else None
        )

        health_counts = defaultdict(int)
        for r in rows:
            health_counts[r["health"]] += 1

        unhealthy_count = sum(
            health_counts.get(h, 0) for h in UNHEALTHY_HEALTHS
        )
        combined_consumed_pct = (
            (total_spend_llm / total_final_budget * 100.0)
            if total_final_budget else 0.0
        )
        combined_health = _health_from_pct(
            combined_consumed_pct, total_final_budget > 0
        )
        run_per_day_total = total_spend_llm / days if days else 0.0
        run_pct_per_day_total = (
            _round2((run_per_day_total / total_final_budget) * 100.0)
            if total_final_budget else 0.0
        )
        estimation_pct = (
            _round2((total_spend_llm / expected_total) * 100.0)
            if expected_total > 0 else 0.0
        )
        budget_amount_pct = (
            _round2((total_spend_llm / total_budget) * 100.0)
            if total_budget else 0.0
        )

        actual_vs_estimated_chart = self._actual_vs_estimated_chart(rows)
        spend_by_model = self._spend_by_model_chart(models_cur)
        phase_budget_chart = self._phase_budget_chart(bids, start, end)

        return {
            **self._portfolio_currency(budgets),
            "filters": {
                "filter_type": filter_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": days,
                "previous_start": prev_start.isoformat(),
                "previous_end": prev_end.isoformat(),
            },
            "kpis": {
                "project_count": project_count,
                "budget_count": len(rows),
                "total_spend_llm": _round2(total_spend_llm),
                "actual_spend": _round2(total_spend_llm),
                "expected_total": _round2(expected_total),
                "estimated_cost": _round2(expected_total),
                "approved_total": _round2(approved_total),
                "total_approved_budget": _round2(approved_total),
                "total_budget": _round2(total_budget),
                "total_topups": _round2(total_topups),
                "project_final_budget": _round2(total_final_budget),
                "actual_vs_estimated_pct": actual_vs_estimated_pct,
                "pct_over_original_budget": pct_over_original_budget,
                "over_budget_project_count": over_budget_project_count,
                "estimation_accuracy_count": estimation_accuracy_count,
                "estimation_accuracy_score": estimation_accuracy_score,
                "health_breakdown": {
                    "healthy": health_counts.get("healthy", 0),
                    "warning": health_counts.get("warning", 0),
                    "at_risk": health_counts.get("at_risk", 0),
                    "critical": health_counts.get("critical", 0),
                    "unknown": health_counts.get("unknown", 0),
                },
            },
            "project_over_estimation": {
                "unhealthy_project_count": unhealthy_count,
                "combined_health": combined_health,
                "combined_consumed_pct": _round2(combined_consumed_pct),
                "project_estimated_amount": _round2(expected_total),
                "actual_spend_amount": _round2(total_spend_llm),
                "estimation_pct": estimation_pct,
                "budget_amount_pct": budget_amount_pct,
                "run_per_day": _round2(run_per_day_total),
                "run_pct_per_day": run_pct_per_day_total,
            },
            "projects": {
                "row_count": len(rows),
                "rows": rows,
            },
            "actual_vs_estimated_chart": {
                "row_count": len(actual_vs_estimated_chart),
                "rows": actual_vs_estimated_chart,
            },
            "spend_by_model": spend_by_model,
            "phase_budget_chart": {
                "row_count": len(phase_budget_chart),
                "rows": phase_budget_chart,
            },
        }

    def _dash_actual_vs_estimated(self, jdata):
        start, end, filter_type = _resolve_window(jdata)
        days = max(1, (end - start).days + 1)
        budgets = self.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
        bids = budgets.ids
        spend = self._spend_by_budget(bids, start, end)
        batches = self._batches_by_budget(bids, start, end)

        rows = []
        for b in budgets:
            bucket = batches.get(b.id) or {"estimated": 0.0, "approved": 0.0}
            estimated = float(bucket["estimated"])
            approved = float(bucket["approved"])
            actual = float(spend.get(b.id, 0.0))
            remaining = approved - actual
            used_pct = (actual / approved * 100.0) if approved > 0 else 0.0
            avg_burn_rate = actual / days

            if approved > 0 and actual > approved:
                health = "over_budget"
            elif estimated > 0 and actual >= estimated * 0.9:
                health = "watch"
            else:
                health = "under_budget"

            rows.append({
                "project_id": (
                    b.ethara_project_id.id if b.ethara_project_id else False
                ),
                "project_name": (
                    b.ethara_project_id.display_name if b.ethara_project_id else ""
                ),
                "budget_id": b.id,
                "budget_name": b.name,
                "actual": _round2(actual),
                "estimated": _round2(estimated),
                "approved": _round2(approved),
                "delta": _round2(actual - estimated),
                "actual_vs_estimated_pct": (
                    _round2((actual / estimated) * 100.0) if estimated > 0 else 0.0
                ),
                "remaining_budget": _round2(remaining),
                "used_pct": _round2(used_pct),
                "avg_burn_rate": _round2(avg_burn_rate),
                "health": health,
            })
        rows.sort(key=lambda r: r["actual"], reverse=True)

        total_actual = sum(r["actual"] for r in rows)
        total_estimated = sum(r["estimated"] for r in rows)
        total_approved = sum(r["approved"] for r in rows)
        total_remaining = sum(r["remaining_budget"] for r in rows)
        total_used_pct = (
            (total_actual / total_approved * 100.0) if total_approved else 0.0
        )
        total_burn_rate = total_actual / days if days else 0.0

        health_counts = {
            "under_budget": sum(1 for r in rows if r["health"] == "under_budget"),
            "watch": sum(1 for r in rows if r["health"] == "watch"),
            "over_budget": sum(1 for r in rows if r["health"] == "over_budget"),
        }

        return {
            **self._portfolio_currency(budgets),
            "filters": {
                "filter_type": filter_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": days,
            },
            "row_count": len(rows),
            "rows": rows,
            "health_counts": health_counts,
            "totals": {
                "actual": _round2(total_actual),
                "estimated": _round2(total_estimated),
                "approved": _round2(total_approved),
                "remaining_budget": _round2(total_remaining),
                "delta": _round2(total_actual - total_estimated),
                "actual_vs_estimated_pct": (
                    _round2((total_actual / total_estimated) * 100.0)
                    if total_estimated > 0 else 0.0
                ),
                "used_pct": _round2(total_used_pct),
                "avg_burn_rate": _round2(total_burn_rate),
            },
        }

    def _dash_spend_by_model(self, jdata):
        start, end, filter_type = _resolve_window(jdata)
        prev_start, prev_end = _previous_window(start, end)
        budgets = self.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
        bids = budgets.ids
        models_cur = self._model_spend_by_budget(bids, start, end)
        models_prev = self._model_spend_by_budget(bids, prev_start, prev_end)
        budget_by_id = {b.id: b for b in budgets}

        current_by_model = defaultdict(float)
        previous_by_model = defaultdict(float)
        for bid_models in models_cur.values():
            for mn, amt in bid_models.items():
                current_by_model[mn] += float(amt or 0.0)
        for bid_models in models_prev.values():
            for mn, amt in bid_models.items():
                previous_by_model[mn] += float(amt or 0.0)

        total_current = sum(current_by_model.values())
        total_previous = sum(previous_by_model.values())
        all_models = set(current_by_model) | set(previous_by_model)

        rows = []
        for mn in sorted(all_models, key=lambda k: current_by_model.get(k, 0.0), reverse=True):
            cur = current_by_model.get(mn, 0.0)
            prev = previous_by_model.get(mn, 0.0)
            change = (
                ((cur - prev) / prev * 100.0) if prev
                else (100.0 if cur else 0.0)
            )
            share = (cur / total_current * 100.0) if total_current else 0.0

            project_breakdown = []
            for bid, bid_models in models_cur.items():
                amt = float(bid_models.get(mn, 0.0))
                if amt <= 0:
                    continue
                budget = budget_by_id.get(bid)
                if not budget:
                    continue
                project_breakdown.append({
                    "project_id": (
                        budget.ethara_project_id.id
                        if budget.ethara_project_id else False
                    ),
                    "project_name": (
                        budget.ethara_project_id.display_name
                        if budget.ethara_project_id else ""
                    ),
                    "budget_id": bid,
                    "budget_name": budget.name,
                    "budget_type": budget.project_type or False,
                    "spend": _round2(amt),
                    "share_in_model_pct": _round2((amt / cur * 100.0) if cur else 0.0),
                })
            project_breakdown.sort(key=lambda r: r["spend"], reverse=True)

            rows.append({
                "model_name": mn,
                "spend": _round2(cur),
                "share_pct": _round2(share),
                "last_month_spend": _round2(prev),
                "change_pct_vs_last_month": _round2(change),
                "project_count": len(project_breakdown),
                "project_breakdown": project_breakdown,
            })

        total_change_pct = (
            ((total_current - total_previous) / total_previous * 100.0)
            if total_previous else (100.0 if total_current else 0.0)
        )

        return {
            **self._portfolio_currency(budgets),
            "filters": {
                "filter_type": filter_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "previous_start": prev_start.isoformat(),
                "previous_end": prev_end.isoformat(),
            },
            "row_count": len(rows),
            "rows": rows,
            "total_spend": _round2(total_current),
            "total_last_month_spend": _round2(total_previous),
            "total_change_pct_vs_last_month": _round2(total_change_pct),
        }

    def _dash_phase_budget(self, jdata):
        start, end, filter_type = _resolve_window(jdata)
        budgets = self.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
        bids = budgets.ids
        rows = self._phase_budget_chart(bids, start, end)

        total_budget = sum(r["budget"] for r in rows)
        total_estimated = sum(r["estimated"] for r in rows)
        total_actual = sum(r["actual"] for r in rows)

        return {
            **self._portfolio_currency(budgets),
            "filters": {
                "filter_type": filter_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "row_count": len(rows),
            "rows": rows,
            "totals": {
                "budget": _round2(total_budget),
                "estimated": _round2(total_estimated),
                "actual": _round2(total_actual),
                "delta_actual_vs_estimated": _round2(total_actual - total_estimated),
                "delta_actual_vs_budget": _round2(total_actual - total_budget),
                "actual_vs_budget_pct": (
                    _round2((total_actual / total_budget) * 100.0)
                    if total_budget else 0.0
                ),
                "actual_vs_estimated_pct": (
                    _round2((total_actual / total_estimated) * 100.0)
                    if total_estimated else 0.0
                ),
            },
        }

    def _dash_burn_rate(self, jdata):
        view = (jdata.get("view") or DEFAULT_BURN_VIEW).lower()
        if view not in BURN_VIEWS:
            raise ValidationError(
                "'view' must be one of: %s" % ", ".join(BURN_VIEWS)
            )
        start, end, filter_type = _burn_resolve_window(jdata, view)
        days = max(1, (end - start).days + 1)
        budgets = self.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
        bids = budgets.ids

        project_meta = {}
        for b in budgets:
            pid = b.ethara_project_id.id if b.ethara_project_id else False
            if pid not in project_meta:
                project_meta[pid] = {
                    "project_id": pid,
                    "project_name": (
                        b.ethara_project_id.display_name
                        if b.ethara_project_id else "(no project)"
                    ),
                }

        budget_to_project = {
            b.id: (b.ethara_project_id.id if b.ethara_project_id else False)
            for b in budgets
        }

        groups = self.env[COST_LINE_MODEL].sudo()._read_group(
            [
                ("budget_id", "in", list(bids)),
                ("granularity", "=", "day"),
                ("is_model_breakdown", "=", False),
                ("period", ">=", start),
                ("period", "<=", end),
            ],
            groupby=["budget_id", "period:day"],
            aggregates=["amount_source:sum"],
        )

        buckets = _enumerate_burn_buckets(start, end, view)
        bucket_keys = [b["key"] for b in buckets]
        bucket_index = {k: i for i, k in enumerate(bucket_keys)}

        bucket_total = defaultdict(float)
        bucket_project = defaultdict(lambda: defaultdict(float))
        project_total = defaultdict(float)
        grand_total = 0.0

        for (bx, period, amount) in groups:
            amt = float(amount or 0.0)
            if amt <= 0:
                continue
            pid = budget_to_project.get(bx.id, False)
            for bkey, weight in _bucket_assignments(period, view):
                if bkey not in bucket_index:
                    continue
                contrib = amt * weight
                bucket_total[bkey] += contrib
                bucket_project[bkey][pid] += contrib
                project_total[pid] += contrib
                grand_total += contrib

        bucket_rows = []
        for b in buckets:
            k = b["key"]
            total_in_bucket = bucket_total.get(k, 0.0)
            proj_rows = []
            for pid, spend in bucket_project.get(k, {}).items():
                meta = project_meta.get(pid) or {
                    "project_id": pid,
                    "project_name": "(no project)",
                }
                proj_rows.append({
                    **meta,
                    "spend": _round2(spend),
                    "share_pct_in_bucket": _round2(
                        (spend / total_in_bucket * 100.0) if total_in_bucket else 0.0
                    ),
                })
            proj_rows.sort(key=lambda r: r["spend"], reverse=True)
            bucket_rows.append({
                "bucket_key": k,
                "bucket_label": b["label"],
                "bucket_start": b["bucket_start"],
                "bucket_end": b["bucket_end"],
                "total": _round2(total_in_bucket),
                "share_pct_of_total": _round2(
                    (total_in_bucket / grand_total * 100.0) if grand_total else 0.0
                ),
                "project_count": len(proj_rows),
                "projects": proj_rows,
            })

        project_totals = []
        for pid, spend in project_total.items():
            meta = project_meta.get(pid) or {
                "project_id": pid,
                "project_name": "(no project)",
            }
            project_totals.append({
                **meta,
                "spend": _round2(spend),
                "share_pct": _round2(
                    (spend / grand_total * 100.0) if grand_total else 0.0
                ),
            })
        project_totals.sort(key=lambda r: r["spend"], reverse=True)

        avg_per_bucket = (
            _round2(grand_total / len(bucket_rows)) if bucket_rows else 0.0
        )
        avg_per_day = _round2(grand_total / days) if days else 0.0

        return {
            **self._portfolio_currency(budgets),
            "filters": {
                "view": view,
                "filter_type": filter_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": days,
                "bucket_unit": BUCKET_UNITS[view],
                "data_source_granularity": "day",
            },
            "row_count": len(bucket_rows),
            "buckets": bucket_rows,
            "totals": {
                "total_spend": _round2(grand_total),
                "bucket_count": len(bucket_rows),
                "avg_per_bucket": avg_per_bucket,
                "avg_per_day": avg_per_day,
                "projects": project_totals,
            },
        }

    def _dash_project_table(self, jdata):
        start, end, filter_type = _resolve_window(jdata)
        days = max(1, (end - start).days + 1)
        budgets = self.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
        bids = budgets.ids
        spend = self._spend_by_budget(bids, start, end)
        model_spend = self._effective_model_spend_by_budget(bids, start, end)
        model_estimated = self._estimated_by_model_for_budgets(bids, start, end)
        total_approved = self._total_approved_by_budget(budgets)
        batches = self._batches_by_budget(bids, start, end)

        projects = defaultdict(list)
        for b in budgets:
            pid = b.ethara_project_id.id if b.ethara_project_id else 0
            projects[pid].append(b)

        rows = []
        for pid, project_budgets in projects.items():
            project_name = ""
            actual = 0.0
            estimated = 0.0
            project_budget_total = 0.0
            model_actuals = defaultdict(float)
            model_estimates = defaultdict(float)
            for b in project_budgets:
                if b.ethara_project_id:
                    project_name = b.ethara_project_id.display_name
                bid = b.id
                project_budget_total += float(total_approved.get(bid, 0.0))
                actual += float(spend.get(bid, 0.0))
                bucket = batches.get(bid) or {"estimated": 0.0}
                estimated += float(bucket["estimated"])
                for mn, amt in (model_spend.get(bid) or {}).items():
                    model_actuals[mn] += float(amt or 0.0)
                for mn, amt in (model_estimated.get(bid) or {}).items():
                    model_estimates[mn] += float(amt or 0.0)

            remaining = project_budget_total - actual
            used_until_pct = (
                (actual / project_budget_total * 100.0)
                if project_budget_total > 0 else 0.0
            )
            run_rate = actual / days

            model_names = set(model_actuals) | set(model_estimates)
            model_total_actual = sum(float(v) for v in model_actuals.values())
            model_rows = []
            for mn in model_names:
                m_actual = float(model_actuals.get(mn, 0.0))
                m_estimated = float(model_estimates.get(mn, 0.0))
                m_run_rate = m_actual / days
                m_share = (
                    (m_actual / model_total_actual * 100.0)
                    if model_total_actual > 0 else 0.0
                )
                m_used_pct = (
                    (m_actual / m_estimated * 100.0)
                    if m_estimated > 0 else 0.0
                )
                model_rows.append({
                    "model_name": mn,
                    "estimated_cost": _round2(m_estimated),
                    "actual_cost": _round2(m_actual),
                    "remaining": 0.0,
                    "project_budget": 0.0,
                    "used_until_pct": _round2(m_used_pct),
                    "run_rate": _round2(m_run_rate),
                    "share_pct": _round2(m_share),
                })
            model_rows.sort(
                key=lambda r: (r["actual_cost"], r["estimated_cost"]),
                reverse=True,
            )

            top_model = model_rows[0]["model_name"] if model_rows else None

            rows.append({
                "project_id": pid,
                "project_name": project_name,
                "estimated_cost": _round2(estimated),
                "actual_cost": _round2(actual),
                "remaining": _round2(remaining),
                "project_budget": _round2(project_budget_total),
                "used_until_pct": _round2(used_until_pct),
                "run_rate": _round2(run_rate),
                "top_model": top_model,
                "model_count": len(model_rows),
                "models": model_rows,
            })
        rows.sort(key=lambda r: r["actual_cost"], reverse=True)

        total_estimated = sum(r["estimated_cost"] for r in rows)
        total_actual = sum(r["actual_cost"] for r in rows)
        total_budget = sum(r["project_budget"] for r in rows)
        total_remaining = total_budget - total_actual
        total_used_pct = (
            (total_actual / total_budget * 100.0) if total_budget > 0 else 0.0
        )
        total_run_rate = total_actual / days if days else 0.0

        return {
            **self._portfolio_currency(budgets),
            "filters": {
                "filter_type": filter_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": days,
            },
            "row_count": len(rows),
            "rows": rows,
            "totals": {
                "estimated_cost": _round2(total_estimated),
                "actual_cost": _round2(total_actual),
                "remaining": _round2(total_remaining),
                "project_budget": _round2(total_budget),
                "used_until_pct": _round2(total_used_pct),
                "run_rate": _round2(total_run_rate),
            },
        }

    def _dash_project_performance_list(self, jdata):
        start, end, filter_type = _resolve_window(jdata)
        days = max(1, (end - start).days + 1)
        budgets = self.env[BUDGET_MODEL].sudo().search([("active", "=", True)])
        bids = budgets.ids
        spend = self._spend_by_budget(bids, start, end)
        total_approved = self._total_approved_by_budget(budgets)
        batches = self._batches_by_budget(bids, start, end)

        projects = defaultdict(list)
        for b in budgets:
            pid = b.ethara_project_id.id if b.ethara_project_id else 0
            projects[pid].append(b)

        rows = []
        for pid, project_budgets in projects.items():
            project_name = ""
            project_budget_total = 0.0
            estimation_budget = 0.0
            actual_budget = 0.0
            for b in project_budgets:
                if b.ethara_project_id:
                    project_name = b.ethara_project_id.display_name
                bid = b.id
                project_budget_total += float(total_approved.get(bid, 0.0))
                actual_budget += float(spend.get(bid, 0.0))
                bucket = batches.get(bid) or {"estimated": 0.0}
                estimation_budget += float(bucket["estimated"])

            variance = estimation_budget - actual_budget

            rows.append({
                "project_id": pid,
                "project_name": project_name,
                "project_budget": _round2(project_budget_total),
                "estimation_budget": _round2(estimation_budget),
                "actual_budget": _round2(actual_budget),
                "variance": _round2(variance),
            })
        rows.sort(key=lambda r: r["actual_budget"], reverse=True)

        total_project_budget = sum(r["project_budget"] for r in rows)
        total_estimation_budget = sum(r["estimation_budget"] for r in rows)
        total_actual_budget = sum(r["actual_budget"] for r in rows)
        total_variance = total_estimation_budget - total_actual_budget

        return {
            **self._portfolio_currency(budgets),
            "filters": {
                "filter_type": filter_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": days,
            },
            "row_count": len(rows),
            "rows": rows,
            "totals": {
                "project_budget": _round2(total_project_budget),
                "estimation_budget": _round2(total_estimation_budget),
                "actual_budget": _round2(total_actual_budget),
                "variance": _round2(total_variance),
            },
        }

    def _dash_phase_health(self, jdata):
        budget_id_raw = jdata.get("budget_id")
        if budget_id_raw in (None, "", False):
            raise ValidationError("'budget_id' is required.")
        try:
            budget_id = int(budget_id_raw)
        except (TypeError, ValueError):
            raise ValidationError("'budget_id' must be an integer.")

        budget = self.env[BUDGET_MODEL].sudo().browse(budget_id).exists()
        if not budget:
            raise ValidationError("Project budget %s not found." % budget_id)

        phase_id_raw = jdata.get("phase_id")
        if phase_id_raw not in (None, "", False):
            try:
                phase_id = int(phase_id_raw)
            except (TypeError, ValueError):
                raise ValidationError("'phase_id' must be an integer.")
            phase = self.env[PHASE_MODEL].sudo().browse(phase_id).exists()
            if not phase or phase.budget_id.id != budget.id:
                raise ValidationError(
                    "Phase %s does not belong to project budget %s."
                    % (phase_id, budget_id)
                )
            phase_source = "explicit"
        else:
            phase = self.env[PHASE_MODEL].sudo().search(
                [
                    ("budget_id", "=", budget.id),
                    ("state", "=", "in_progress"),
                ],
                order="start_date desc, id desc",
                limit=1,
            )
            if not phase:
                phase = self.env[PHASE_MODEL].sudo().search(
                    [
                        ("budget_id", "=", budget.id),
                        ("state", "in", list(ACTIVE_PHASE_STATES)),
                    ],
                    order="start_date desc, id desc",
                    limit=1,
                )
            if not phase:
                raise ValidationError(
                    "No running phase found for project budget %s." % budget_id
                )
            phase_source = "current_running"

        budget_amount = float(budget.budget_amount or 0.0)
        topup_total = float(self._topups_by_budget([budget.id]).get(budget.id, 0.0))
        project_final_budget = float(
            self._total_approved_by_budget(budget).get(budget.id, 0.0)
        )

        estimated = float(phase.estimated_cost or 0.0)
        approved = float(phase.approved_amount or 0.0)
        buffer_pct = float(phase.buffer_pct or 0.0)
        phase_budget_total = float(phase.phase_budget or 0.0)
        buffer_amount = phase_budget_total - estimated

        start_date = phase.start_date
        end_date = phase.end_date
        today = date.today()

        if start_date and end_date:
            total_days = max(1, (end_date - start_date).days + 1)
        elif start_date:
            total_days = max(1, (today - start_date).days + 1)
        else:
            total_days = 1

        if start_date:
            effective_end = today if (not end_date or today < end_date) else end_date
            if effective_end < start_date:
                effective_end = start_date
            elapsed_days = max(1, (effective_end - start_date).days + 1)
        else:
            effective_end = None
            elapsed_days = 1

        if end_date and today < end_date:
            remaining_days = (end_date - today).days
        elif end_date:
            remaining_days = 0
        else:
            remaining_days = 0

        if start_date and effective_end:
            actual = float(
                self._spend_by_budget([budget.id], start_date, effective_end).get(
                    budget.id, 0.0
                )
            )
        else:
            actual = 0.0

        actual_burn_per_day = actual / elapsed_days if elapsed_days else 0.0
        estimated_burn_per_day = estimated / total_days if total_days else 0.0
        burn_rate_delta_per_day = actual_burn_per_day - estimated_burn_per_day

        forecasted_remaining_spend = actual_burn_per_day * remaining_days
        forecasted_total_spend = actual + forecasted_remaining_spend
        forecasted_over_phase_budget = max(0.0, forecasted_total_spend - approved)
        forecasted_over_project_budget = max(
            0.0, forecasted_total_spend - project_final_budget
        )
        predicted_variance = forecasted_total_spend - estimated

        pct_budget = (actual / approved * 100.0) if approved > 0 else 0.0
        pct_estimation = (actual / estimated * 100.0) if estimated > 0 else 0.0
        estimated_pct_of_actual = (
            (estimated / actual * 100.0) if actual > 0 else 0.0
        )
        actual_pct_of_estimated = pct_estimation
        buffer_pct_of_estimated = (
            (buffer_amount / estimated * 100.0) if estimated > 0 else 0.0
        )

        if approved > 0:
            forecast_pct = forecasted_total_spend / approved * 100.0
            over_pct = max(0.0, forecast_pct - 100.0)
            health_score = _round2(max(0.0, 100.0 - over_pct))
        else:
            forecast_pct = 0.0
            health_score = None

        phase_health = _health_from_pct(
            (actual / approved * 100.0) if approved > 0 else 0.0,
            approved > 0,
        )

        return {
            **self._portfolio_currency(budget),
            "budget": {
                "budget_id": budget.id,
                "budget_name": budget.name,
                "project_id": (
                    budget.ethara_project_id.id if budget.ethara_project_id else False
                ),
                "project_name": (
                    budget.ethara_project_id.display_name
                    if budget.ethara_project_id else ""
                ),
                "budget_type": budget.project_type or False,
                "project_budget": _round2(budget_amount),
                "topup_total": _round2(topup_total),
                "project_final_budget": _round2(project_final_budget),
            },
            "phase": {
                "phase_id": phase.id,
                "phase_name": phase.name,
                "state": phase.state,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "source": phase_source,
                "total_days": total_days,
                "elapsed_days": elapsed_days,
                "remaining_days": remaining_days,
            },
            "metrics": {
                "project_final_budget": _round2(project_final_budget),
                "actual_cost": _round2(actual),
                "estimated_cost": _round2(estimated),
                "approved_amount": _round2(approved),
                "pct_budget": _round2(pct_budget),
                "pct_estimation": _round2(pct_estimation),
                "actual_pct_of_estimated": _round2(actual_pct_of_estimated),
                "estimated_pct_of_actual": _round2(estimated_pct_of_actual),
                "buffer_amount": _round2(buffer_amount),
                "buffer_pct": _round2(buffer_pct),
                "buffer_pct_of_estimated": _round2(buffer_pct_of_estimated),
            },
            "burn_rate": {
                "actual_per_day": _round2(actual_burn_per_day),
                "estimated_per_day": _round2(estimated_burn_per_day),
                "delta_per_day": _round2(burn_rate_delta_per_day),
                "delta_pct_vs_estimated": (
                    _round2(
                        (burn_rate_delta_per_day / estimated_burn_per_day) * 100.0
                    )
                    if estimated_burn_per_day > 0 else 0.0
                ),
            },
            "forecast": {
                "forecasted_remaining_spend": _round2(forecasted_remaining_spend),
                "forecasted_total_spend": _round2(forecasted_total_spend),
                "forecast_pct_of_approved": _round2(forecast_pct),
                "forecasted_over_phase_budget": _round2(forecasted_over_phase_budget),
                "forecasted_over_project_budget": _round2(
                    forecasted_over_project_budget
                ),
                "predicted_variance": _round2(predicted_variance),
            },
            "health": {
                "phase_health": phase_health,
                "health_score": health_score,
            },
        }
