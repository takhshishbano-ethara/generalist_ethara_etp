import calendar
import logging
from collections import defaultdict
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

HEALTH_SELECTION = [
    ("unknown", "Unknown"),
    ("healthy", "Healthy"),
    ("warning", "Warning"),
    ("at_risk", "At Risk"),
    ("critical", "Critical"),
]

HEALTHY_PCT = 60.0
WARNING_PCT = 80.0
AT_RISK_PCT = 100.0

ACTIVE_BUDGET_STATES = ("approved", "in_progress", "delivered", "closed")
ACTIVE_BATCH_STATES = ACTIVE_BUDGET_STATES


def _month_start(d):
    if not d:
        return None
    return d.replace(day=1)


def _month_end(d):
    if not d:
        return None
    start = _month_start(d)
    return (start + relativedelta(months=1)) - relativedelta(days=1)


def _days_overlap(range_start, range_end, window_start, window_end):
    s = max(range_start, window_start)
    e = min(range_end, window_end)
    if e < s:
        return 0
    return (e - s).days + 1


class EtpProjectBudgetDashboard(models.Model):
    _name = "etp.project.budget.dashboard"
    _description = "Project Budget Dashboard Rollup (project x month)"
    _order = "period_month desc, project_id"
    _rec_name = "display_name"

    project_id = fields.Many2one(
        "project.project",
        required=True,
        ondelete="cascade",
        index=True,
    )
    period_month = fields.Date(
        required=True,
        index=True,
        help="First day of the month this row aggregates.",
    )
    month_label = fields.Char(
        compute="_compute_month_label", store=True, index=True,
    )
    display_name = fields.Char(
        compute="_compute_display_name", store=True,
    )

    project_classification = fields.Selection(
        related="project_id.project_classification",
        store=True,
        index=True,
    )

    has_rnd_budget = fields.Boolean(
        compute="_compute_budget_type_flags", store=True, index=True,
    )
    has_operations_budget = fields.Boolean(
        compute="_compute_budget_type_flags", store=True, index=True,
    )
    budget_type_label = fields.Selection(
        [
            ("rnd", "R&D"),
            ("operations", "Operations"),
            ("mixed", "Mixed"),
            ("unclassified", "Unclassified"),
        ],
        compute="_compute_budget_type_flags",
        store=True,
        index=True,
    )

    actual_cost = fields.Float(
        compute="_compute_actual_cost", store=True,
        help="LLM cost lines (granularity=day, service-level) for this project in this month.",
    )
    estimation_cost = fields.Float(
        compute="_compute_estimation_cost", store=True,
        help="Sum of phase daily-task totals for this project in this month.",
    )
    budget_cost = fields.Float(
        compute="_compute_budget_cost", store=True,
        help="Approved phase budgets prorated to this month + subscription/infra monthly cost.",
    )

    variance_budget_vs_actual = fields.Float(
        compute="_compute_variances", store=True,
    )
    variance_budget_vs_estimation = fields.Float(
        compute="_compute_variances", store=True,
    )
    variance_estimation_vs_actual = fields.Float(
        compute="_compute_variances", store=True,
    )
    consumed_pct = fields.Float(
        compute="_compute_variances", store=True,
        help="Actual / Budget * 100.",
    )

    health_status = fields.Selection(
        HEALTH_SELECTION,
        compute="_compute_health_status", store=True, index=True,
        default="unknown",
    )

    done_tasks_in_month = fields.Integer(
        compute="_compute_task_counters", store=True,
    )
    total_tasks_in_month = fields.Integer(
        compute="_compute_task_counters", store=True,
    )

    currency_id = fields.Many2one(
        "res.currency",
        compute="_compute_currency_id",
        store=False,
    )

    _sql_constraints = [
        (
            "uniq_project_period",
            "unique(project_id, period_month)",
            "One dashboard row per project + month.",
        ),
    ]

    @api.depends("period_month")
    def _compute_month_label(self):
        for rec in self:
            rec.month_label = rec.period_month.strftime("%Y-%m") if rec.period_month else ""

    @api.depends("project_id.display_name", "month_label")
    def _compute_display_name(self):
        for rec in self:
            pname = rec.project_id.display_name or ""
            rec.display_name = f"{pname} - {rec.month_label}" if pname else rec.month_label

    @api.depends("project_id")
    def _compute_currency_id(self):
        usd = self.env.ref("base.USD", raise_if_not_found=False)
        for rec in self:
            rec.currency_id = usd.id if usd else False

    @api.depends("project_id")
    def _compute_budget_type_flags(self):
        Budget = self.env["etp.project.aws.budget"].sudo()
        for rec in self:
            budgets = Budget.search([
                ("project_id", "=", rec.project_id.id),
                ("active", "=", True),
            ])
            has_rnd = any(b.project_type == "rnd" for b in budgets)
            has_ops = any(b.project_type == "operations" for b in budgets)
            rec.has_rnd_budget = has_rnd
            rec.has_operations_budget = has_ops
            if has_rnd and has_ops:
                rec.budget_type_label = "mixed"
            elif has_rnd:
                rec.budget_type_label = "rnd"
            elif has_ops:
                rec.budget_type_label = "operations"
            else:
                rec.budget_type_label = "unclassified"

    @api.depends("project_id", "period_month")
    def _compute_actual_cost(self):
        Line = self.env["etp.project.aws.cost.line"].sudo()
        for rec in self:
            if not (rec.project_id and rec.period_month):
                rec.actual_cost = 0.0
                continue
            start = _month_start(rec.period_month)
            end = _month_end(rec.period_month)
            lines = Line.search([
                ("project_id", "=", rec.project_id.id),
                ("granularity", "=", "day"),
                ("is_model_breakdown", "=", False),
                ("period", ">=", start),
                ("period", "<=", end),
            ])
            rec.actual_cost = sum(float(l.amount_source or 0.0) for l in lines)

    @api.depends("project_id", "period_month")
    def _compute_estimation_cost(self):
        DailyTask = self.env["etp.batch.budget.daily.task"].sudo()
        for rec in self:
            if not (rec.project_id and rec.period_month):
                rec.estimation_cost = 0.0
                continue
            start = _month_start(rec.period_month)
            end = _month_end(rec.period_month)
            tasks = DailyTask.search([
                ("batch_id.project_id", "=", rec.project_id.id),
                ("entry_date", ">=", start),
                ("entry_date", "<=", end),
            ])
            rec.estimation_cost = sum(
                float(t.total_cost or 0.0)
                + float(t.infra_cost or 0.0)
                + float(t.subscription_cost or 0.0)
                for t in tasks
            )

    @api.depends("project_id", "period_month")
    def _compute_budget_cost(self):
        Batch = self.env["etp.batch.budget"].sudo()
        MonthlyCost = self.env["etp.project.budget.monthly.cost"].sudo()
        for rec in self:
            if not (rec.project_id and rec.period_month):
                rec.budget_cost = 0.0
                continue
            month_start = _month_start(rec.period_month)
            month_end = _month_end(rec.period_month)
            days_in_month = (month_end - month_start).days + 1

            batches = Batch.search([
                ("project_id", "=", rec.project_id.id),
                ("state", "in", list(ACTIVE_BATCH_STATES)),
                ("start_date", "<=", month_end),
                ("end_date", ">=", month_start),
            ])
            phase_total = 0.0
            for bx in batches:
                if not (bx.start_date and bx.end_date):
                    continue
                phase_days = (bx.end_date - bx.start_date).days + 1
                if phase_days <= 0:
                    continue
                overlap = _days_overlap(
                    bx.start_date, bx.end_date, month_start, month_end,
                )
                if overlap <= 0:
                    continue
                phase_total += float(bx.estimated_cost or 0.0) * (overlap / phase_days)

            monthly_rows = MonthlyCost.search([
                ("budget_id.project_id", "=", rec.project_id.id),
                ("period_month", "=", month_start),
            ])
            monthly_total = sum(
                float(m.per_day_avg_cost or 0.0) * days_in_month
                for m in monthly_rows
            )

            rec.budget_cost = phase_total + monthly_total

    @api.depends("actual_cost", "estimation_cost", "budget_cost")
    def _compute_variances(self):
        for rec in self:
            rec.variance_budget_vs_actual = rec.budget_cost - rec.actual_cost
            rec.variance_budget_vs_estimation = rec.budget_cost - rec.estimation_cost
            rec.variance_estimation_vs_actual = rec.estimation_cost - rec.actual_cost
            if rec.budget_cost:
                rec.consumed_pct = (rec.actual_cost / rec.budget_cost) * 100.0
            else:
                rec.consumed_pct = 0.0

    @api.depends("consumed_pct", "budget_cost")
    def _compute_health_status(self):
        for rec in self:
            if not rec.budget_cost:
                rec.health_status = "unknown"
            elif rec.consumed_pct < HEALTHY_PCT:
                rec.health_status = "healthy"
            elif rec.consumed_pct < WARNING_PCT:
                rec.health_status = "warning"
            elif rec.consumed_pct < AT_RISK_PCT:
                rec.health_status = "at_risk"
            else:
                rec.health_status = "critical"

    @api.depends("project_id", "period_month")
    def _compute_task_counters(self):
        DailyTask = self.env["etp.batch.budget.daily.task"].sudo()
        for rec in self:
            if not (rec.project_id and rec.period_month):
                rec.done_tasks_in_month = 0
                rec.total_tasks_in_month = 0
                continue
            start = _month_start(rec.period_month)
            end = _month_end(rec.period_month)
            tasks = DailyTask.search([
                ("batch_id.project_id", "=", rec.project_id.id),
                ("entry_date", ">=", start),
                ("entry_date", "<=", end),
            ])
            done = sum(int(t.done_count or 0) for t in tasks)
            rec.done_tasks_in_month = done
            rec.total_tasks_in_month = done

    @api.model
    def _ensure_row(self, project_id, period_month):
        if not (project_id and period_month):
            return self.browse()
        ms = _month_start(period_month)
        existing = self.sudo().search([
            ("project_id", "=", project_id),
            ("period_month", "=", ms),
        ], limit=1)
        if existing:
            return existing
        return self.sudo().create({
            "project_id": project_id,
            "period_month": ms,
        })

    @api.model
    def _ensure_rows_bulk(self, pairs):
        if not pairs:
            return self.browse()
        normalized = set()
        for pid, pm in pairs:
            if pid and pm:
                normalized.add((pid, _month_start(pm)))
        if not normalized:
            return self.browse()
        project_ids = {pid for pid, _ in normalized}
        months = {pm for _, pm in normalized}
        existing = self.sudo().search([
            ("project_id", "in", list(project_ids)),
            ("period_month", "in", list(months)),
        ])
        have = {(r.project_id.id, r.period_month) for r in existing}
        to_create = [
            {"project_id": pid, "period_month": pm}
            for pid, pm in normalized
            if (pid, pm) not in have
        ]
        if to_create:
            created = self.sudo().create(to_create)
            existing |= created
        return existing

    @api.model
    def _recompute_for(self, project_ids=None, months=None):
        domain = []
        if project_ids:
            domain.append(("project_id", "in", list(project_ids)))
        if months:
            domain.append(("period_month", "in", [_month_start(m) for m in months]))
        rows = self.sudo().search(domain) if domain else self.sudo().search([])
        if not rows:
            return rows
        rows._compute_actual_cost()
        rows._compute_estimation_cost()
        rows._compute_budget_cost()
        rows._compute_variances()
        rows._compute_health_status()
        rows._compute_task_counters()
        rows._compute_budget_type_flags()
        return rows

    @api.model
    def get_dashboard_data(self, filters=None):
        filters = filters or {}
        domain = []
        project_ids = filters.get("project_ids") or []
        if project_ids:
            domain.append(("project_id", "in", project_ids))
        classification = filters.get("project_classification")
        if classification in ("internal", "external"):
            domain.append(("project_classification", "=", classification))
        budget_type = filters.get("budget_type")
        if budget_type == "rnd":
            domain.append(("has_rnd_budget", "=", True))
        elif budget_type == "operations":
            domain.append(("has_operations_budget", "=", True))
        start_month = filters.get("start_month")
        end_month = filters.get("end_month")
        if start_month:
            domain.append(("period_month", ">=", _month_start(fields.Date.from_string(start_month))))
        if end_month:
            domain.append(("period_month", "<=", _month_start(fields.Date.from_string(end_month))))

        rows = self.sudo().search(domain)
        usd = self.env.ref("base.USD", raise_if_not_found=False)
        currency_symbol = usd.symbol if usd else "$"

        row_dicts = []
        for r in rows:
            row_dicts.append({
                "id": r.id,
                "project_id": r.project_id.id,
                "project_name": r.project_id.display_name or "",
                "period_month": r.period_month.isoformat() if r.period_month else "",
                "month_label": r.month_label,
                "project_classification": r.project_classification or "",
                "budget_type_label": r.budget_type_label or "unclassified",
                "has_rnd_budget": r.has_rnd_budget,
                "has_operations_budget": r.has_operations_budget,
                "actual_cost": round(r.actual_cost or 0.0, 2),
                "estimation_cost": round(r.estimation_cost or 0.0, 2),
                "budget_cost": round(r.budget_cost or 0.0, 2),
                "variance_budget_vs_actual": round(r.variance_budget_vs_actual or 0.0, 2),
                "variance_budget_vs_estimation": round(r.variance_budget_vs_estimation or 0.0, 2),
                "variance_estimation_vs_actual": round(r.variance_estimation_vs_actual or 0.0, 2),
                "consumed_pct": round(r.consumed_pct or 0.0, 2),
                "health_status": r.health_status or "unknown",
                "done_tasks_in_month": r.done_tasks_in_month,
                "total_tasks_in_month": r.total_tasks_in_month,
            })

        total_actual = sum(r["actual_cost"] for r in row_dicts)
        total_estimation = sum(r["estimation_cost"] for r in row_dicts)
        total_budget = sum(r["budget_cost"] for r in row_dicts)
        total_variance_ba = total_budget - total_actual
        total_variance_be = total_budget - total_estimation
        total_consumed_pct = (total_actual / total_budget * 100.0) if total_budget else 0.0
        if not total_budget:
            overall_health = "unknown"
        elif total_consumed_pct < HEALTHY_PCT:
            overall_health = "healthy"
        elif total_consumed_pct < WARNING_PCT:
            overall_health = "warning"
        elif total_consumed_pct < AT_RISK_PCT:
            overall_health = "at_risk"
        else:
            overall_health = "critical"

        health_breakdown = defaultdict(int)
        for r in row_dicts:
            health_breakdown[r["health_status"]] += 1

        by_month = defaultdict(lambda: {"actual": 0.0, "estimation": 0.0, "budget": 0.0})
        for r in row_dicts:
            key = r["month_label"]
            by_month[key]["actual"] += r["actual_cost"]
            by_month[key]["estimation"] += r["estimation_cost"]
            by_month[key]["budget"] += r["budget_cost"]
        variance_by_month = [
            {
                "month_label": k,
                "actual": round(v["actual"], 2),
                "estimation": round(v["estimation"], 2),
                "budget": round(v["budget"], 2),
                "variance_ba": round(v["budget"] - v["actual"], 2),
            }
            for k, v in sorted(by_month.items())
        ]

        by_project = defaultdict(lambda: {"actual": 0.0, "estimation": 0.0, "budget": 0.0, "name": ""})
        for r in row_dicts:
            slot = by_project[r["project_id"]]
            slot["name"] = r["project_name"]
            slot["actual"] += r["actual_cost"]
            slot["estimation"] += r["estimation_cost"]
            slot["budget"] += r["budget_cost"]
        project_leaderboard = sorted(
            [
                {
                    "project_id": pid,
                    "project_name": v["name"],
                    "actual": round(v["actual"], 2),
                    "estimation": round(v["estimation"], 2),
                    "budget": round(v["budget"], 2),
                    "consumed_pct": round((v["actual"] / v["budget"] * 100.0) if v["budget"] else 0.0, 2),
                }
                for pid, v in by_project.items()
            ],
            key=lambda r: r["actual"],
            reverse=True,
        )

        budget_type_split = defaultdict(lambda: {"actual": 0.0, "budget": 0.0, "count": 0})
        for r in row_dicts:
            slot = budget_type_split[r["budget_type_label"]]
            slot["actual"] += r["actual_cost"]
            slot["budget"] += r["budget_cost"]
            slot["count"] += 1
        budget_type_chart = [
            {
                "budget_type": k,
                "actual": round(v["actual"], 2),
                "budget": round(v["budget"], 2),
                "row_count": v["count"],
            }
            for k, v in budget_type_split.items()
        ]

        Project = self.env["project.project"].sudo()
        all_projects = Project.search([], order="name")
        projects_filter = [
            {
                "id": p.id,
                "name": p.display_name or "",
                "classification": p.project_classification or "",
            }
            for p in all_projects
        ]

        months_filter = sorted(
            {r["month_label"] for r in row_dicts if r["month_label"]}
        )

        return {
            "currency_symbol": currency_symbol,
            "totals": {
                "actual_cost": round(total_actual, 2),
                "estimation_cost": round(total_estimation, 2),
                "budget_cost": round(total_budget, 2),
                "variance_budget_vs_actual": round(total_variance_ba, 2),
                "variance_budget_vs_estimation": round(total_variance_be, 2),
                "consumed_pct": round(total_consumed_pct, 2),
                "overall_health": overall_health,
                "row_count": len(row_dicts),
                "project_count": len(by_project),
            },
            "health_breakdown": {
                "healthy": health_breakdown.get("healthy", 0),
                "warning": health_breakdown.get("warning", 0),
                "at_risk": health_breakdown.get("at_risk", 0),
                "critical": health_breakdown.get("critical", 0),
                "unknown": health_breakdown.get("unknown", 0),
            },
            "rows": row_dicts,
            "variance_by_month": variance_by_month,
            "project_leaderboard": project_leaderboard,
            "budget_type_chart": budget_type_chart,
            "filter_options": {
                "projects": projects_filter,
                "months": months_filter,
                "classifications": ["internal", "external"],
                "budget_types": ["rnd", "operations"],
            },
            "applied_filters": {
                "project_ids": project_ids,
                "project_classification": classification or "",
                "budget_type": budget_type or "",
                "start_month": start_month or "",
                "end_month": end_month or "",
            },
        }

    @api.model
    def action_refresh_all(self):
        rows = self.sudo().search([])
        if rows:
            self._recompute_for(
                project_ids=rows.mapped("project_id").ids,
                months=rows.mapped("period_month"),
            )
        return True


class EtpProjectAwsCostLineDashboardHook(models.Model):
    _inherit = "etp.project.aws.cost.line"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_dashboard_rows()
        return records

    def write(self, vals):
        before = [(r.project_id.id, r.period) for r in self]
        res = super().write(vals)
        if {"period", "amount_source", "granularity", "is_model_breakdown", "budget_id"} & set(vals or {}):
            self._sync_dashboard_rows(extra_pairs=before)
        return res

    def _sync_dashboard_rows(self, extra_pairs=None):
        Dashboard = self.env["etp.project.budget.dashboard"].sudo()
        pairs = set()
        for r in self:
            if r.granularity == "day" and r.project_id and r.period:
                pairs.add((r.project_id.id, _month_start(r.period)))
        for pid, period in (extra_pairs or []):
            if pid and period:
                pairs.add((pid, _month_start(period)))
        if not pairs:
            return
        Dashboard._ensure_rows_bulk(pairs)
        Dashboard._recompute_for(
            project_ids={p for p, _ in pairs},
            months={m for _, m in pairs},
        )


class EtpBatchBudgetDailyTaskDashboardHook(models.Model):
    _inherit = "etp.batch.budget.daily.task"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_dashboard_rows()
        return records

    def write(self, vals):
        before = [(r.batch_id.project_id.id, r.entry_date) for r in self]
        res = super().write(vals)
        if {"entry_date", "done_count", "total_cost", "infra_cost", "subscription_cost", "batch_id"} & set(vals or {}):
            self._sync_dashboard_rows(extra_pairs=before)
        return res

    def _sync_dashboard_rows(self, extra_pairs=None):
        Dashboard = self.env["etp.project.budget.dashboard"].sudo()
        pairs = set()
        for r in self:
            pid = r.batch_id.project_id.id if r.batch_id and r.batch_id.project_id else False
            if pid and r.entry_date:
                pairs.add((pid, _month_start(r.entry_date)))
        for pid, entry in (extra_pairs or []):
            if pid and entry:
                pairs.add((pid, _month_start(entry)))
        if not pairs:
            return
        Dashboard._ensure_rows_bulk(pairs)
        Dashboard._recompute_for(
            project_ids={p for p, _ in pairs},
            months={m for _, m in pairs},
        )


class EtpProjectBudgetMonthlyCostDashboardHook(models.Model):
    _inherit = "etp.project.budget.monthly.cost"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_dashboard_rows()
        return records

    def write(self, vals):
        before = [(r.budget_id.project_id.id, r.period_month) for r in self]
        res = super().write(vals)
        if {"period_month", "monthly_cost", "basis", "budget_id"} & set(vals or {}):
            self._sync_dashboard_rows(extra_pairs=before)
        return res

    def _sync_dashboard_rows(self, extra_pairs=None):
        Dashboard = self.env["etp.project.budget.dashboard"].sudo()
        pairs = set()
        for r in self:
            pid = r.budget_id.project_id.id if r.budget_id and r.budget_id.project_id else False
            if pid and r.period_month:
                pairs.add((pid, _month_start(r.period_month)))
        for pid, pm in (extra_pairs or []):
            if pid and pm:
                pairs.add((pid, _month_start(pm)))
        if not pairs:
            return
        Dashboard._ensure_rows_bulk(pairs)
        Dashboard._recompute_for(
            project_ids={p for p, _ in pairs},
            months={m for _, m in pairs},
        )


class EtpBatchBudgetDashboardHook(models.Model):
    _inherit = "etp.batch.budget"

    def write(self, vals):
        res = super().write(vals)
        if {"state", "estimated_cost", "approved_amount", "start_date", "end_date", "project_id"} & set(vals or {}):
            self._sync_dashboard_rows()
        return res

    def _sync_dashboard_rows(self):
        Dashboard = self.env["etp.project.budget.dashboard"].sudo()
        pairs = set()
        for bx in self:
            if not (bx.project_id and bx.start_date and bx.end_date):
                continue
            cursor = _month_start(bx.start_date)
            end_month = _month_start(bx.end_date)
            while cursor <= end_month:
                pairs.add((bx.project_id.id, cursor))
                cursor = cursor + relativedelta(months=1)
        if not pairs:
            return
        Dashboard._ensure_rows_bulk(pairs)
        Dashboard._recompute_for(
            project_ids={p for p, _ in pairs},
            months={m for _, m in pairs},
        )
