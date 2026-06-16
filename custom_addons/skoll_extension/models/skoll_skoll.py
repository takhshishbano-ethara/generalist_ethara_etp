import logging
from datetime import date, datetime

from odoo import api, models

_logger = logging.getLogger(__name__)

DONE_STATES = ("passed",)
IN_PROGRESS_STATES = ()

APPROVED_VERDICTS = ("passed",)
REWORK_VERDICTS = ("failed",)
DECIDED_VERDICTS = APPROVED_VERDICTS + REWORK_VERDICTS


SKOLL_FULL_ACCESS_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
    "api_auth_gateway.role_pl_technical",
    "api_auth_gateway.role_pl_stem",
    "api_auth_gateway.role_pl_non_stem",
)

SKOLL_QL_ROLE_XMLIDS = (
    "api_auth_gateway.role_qc_technical",
    "api_auth_gateway.role_qc_stem",
    "api_auth_gateway.role_qc_non_stem",
)

SKOLL_TASKER_ROLE_XMLIDS = (
    "api_auth_gateway.role_tasker_technical",
    "api_auth_gateway.role_tasker_stem",
    "api_auth_gateway.role_tasker_non_stem",
)


def _get_role_ids(env, xmlids):
    ids = []
    for xmlid in xmlids:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


def _user_has_role(env, xmlids):
    role = env.user.user_role
    if not role:
        return False
    return role.id in _get_role_ids(env, xmlids)


def _user_role_tag(env):
    if _user_has_role(env, SKOLL_FULL_ACCESS_ROLE_XMLIDS):
        return "full"
    if _user_has_role(env, SKOLL_QL_ROLE_XMLIDS):
        return "ql"
    if _user_has_role(env, SKOLL_TASKER_ROLE_XMLIDS):
        return "tasker"
    return None


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _to_iso_day(raw):
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(str(raw), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


class SkollSkoll(models.Model):
    _inherit = "skoll.skoll"

    @api.model
    def _performance_scope_domain(self):
        tag = _user_role_tag(self.env)
        if tag in ("full", "ql"):
            return []
        if tag == "tasker":
            return [("employee_id.user_id", "=", self.env.user.id)]
        return [("id", "=", 0)]

    @api.model
    def get_performance_metrics(self):
        domain = self._performance_scope_domain()
        Skoll = self.sudo()
        Gen = self.env["skoll.generation"].sudo()

        total_task_count = Skoll.search_count(domain)
        task_done = Skoll.search_count(
            domain + [("qc_status", "in", list(DONE_STATES))]
        )

        reviewed_count = Skoll.search_count(
            domain + [("qc_status", "in", list(DECIDED_VERDICTS))]
        )
        approved_count = Skoll.search_count(
            domain + [("qc_status", "in", list(APPROVED_VERDICTS))]
        )
        rework_count = Skoll.search_count(
            domain + [("qc_status", "in", list(REWORK_VERDICTS))]
        )

        tasks = Skoll.search(domain)
        gen_aht_domain = [("task_id", "in", tasks.ids), ("duration_s", ">", 0)]
        aht_measured_count = Gen.search_count(gen_aht_domain)
        aht_groups = Gen._read_group(gen_aht_domain, [], ["duration_s:avg"])
        avg_seconds = (aht_groups[0][0] if aht_groups else 0.0) or 0.0

        gen_cost_domain = [("task_id", "in", tasks.ids), ("total_cost", ">", 0)]
        cost_measured_count = Gen.search_count(gen_cost_domain)
        cost_groups = Gen._read_group(
            gen_cost_domain, [], ["total_cost:sum", "total_cost:avg"]
        )
        if cost_groups:
            total_cost, avg_cost = cost_groups[0]
        else:
            total_cost, avg_cost = 0.0, 0.0
        total_cost = total_cost or 0.0
        avg_cost = avg_cost or 0.0

        return {
            "total_task_count": total_task_count,
            "task_done": task_done,
            "approval_percentage": _pct(approved_count, reviewed_count),
            "rework_percentage": _pct(rework_count, reviewed_count),
            "avg_handling_time_seconds": round(avg_seconds, 2),
            "avg_handling_time_minutes": round(avg_seconds / 60.0, 2),
            "approved_count": approved_count,
            "rework_count": rework_count,
            "reviewed_count": reviewed_count,
            "aht_measured_count": aht_measured_count,
            "total_cost_usd": round(total_cost, 6),
            "average_cost_usd": round(avg_cost, 6),
            "cost_measured_count": cost_measured_count,
        }

    @api.model
    def get_list_metrics(self):
        """Lightweight subset of :meth:`get_performance_metrics` for the
        project-list endpoint: only the total / done / aht-measured counts it
        actually displays. Skips the QC and cost aggregation the list throws
        away. ``aht_measured_count`` keeps the same skoll.generation join as the
        full method; when scope is unrestricted we count generations directly
        instead of loading every skoll task id first."""
        domain = self._performance_scope_domain()
        Skoll = self.sudo()
        Gen = self.env["skoll.generation"].sudo()

        total_task_count = Skoll.search_count(domain)
        task_done = Skoll.search_count(domain + [("qc_status", "in", list(DONE_STATES))])

        if domain:
            task_ids = Skoll.search(domain).ids
            gen_aht_domain = [("task_id", "in", task_ids), ("duration_s", ">", 0)]
        else:
            gen_aht_domain = [("duration_s", ">", 0)]
        aht_measured_count = Gen.search_count(gen_aht_domain)

        return {
            "total_task_count": total_task_count,
            "task_done": task_done,
            "aht_measured_count": aht_measured_count,
        }

    @api.model
    def get_tasks_completed_timeseries(self, dt_from, dt_to):
        domain = [
            ("qc_status", "in", list(DONE_STATES)),
            ("write_date", ">=", dt_from),
            ("write_date", "<=", dt_to),
        ]
        rows = self.sudo().read_group(
            domain=domain,
            fields=["write_date"],
            groupby=["write_date:day"],
            lazy=False,
        )
        out = []
        for row in rows:
            raw = row.get("write_date:day") or row.get("write_date")
            count = int(row.get("__count") or 0)
            if not raw or not count:
                continue
            iso = _to_iso_day(raw)
            if not iso:
                continue
            out.append((iso, count))
        return out

    @api.model
    def get_daily_burn_timeseries(self, dt_from, dt_to):
        Gen = self.env["skoll.generation"].sudo()
        domain = [
            ("total_cost", ">", 0),
            ("create_date", ">=", dt_from),
            ("create_date", "<=", dt_to),
        ]
        rows = Gen.read_group(
            domain=domain,
            fields=["create_date", "total_cost:sum"],
            groupby=["create_date:day"],
            lazy=False,
        )
        out = []
        for row in rows:
            raw = row.get("create_date:day") or row.get("create_date")
            cost = float(row.get("total_cost") or 0.0)
            if not raw or cost <= 0:
                continue
            iso = _to_iso_day(raw)
            if not iso:
                continue
            out.append((iso, cost))
        return out

    @api.model
    def get_main_dashboard_metrics(
        self, today_from, today_to, yesterday_from, yesterday_to
    ):
        """Return raw founder-summary metrics from this task table.

        Every key is always present so the controller can aggregate
        uniformly across heterogeneous connected tables:

            in_progress_user_ids: res.users IDs currently running tasks
            has_in_progress_work: any in-progress row exists right now
            completed_today:      done rows with write ts today
            completed_yesterday:  done rows with write ts yesterday
            overdue_task_count:   0 (no overdue state on skoll.skoll)
        """
        Skoll = self.sudo()
        in_progress = Skoll.search(
            [("qc_status", "in", list(IN_PROGRESS_STATES))]
        )
        in_progress_user_ids = list(set(in_progress.mapped("employee_id.user_id").ids))
        completed_today = Skoll.search_count([
            ("qc_status", "in", list(DONE_STATES)),
            ("write_date", ">=", today_from),
            ("write_date", "<=", today_to),
        ])
        completed_yesterday = Skoll.search_count([
            ("qc_status", "in", list(DONE_STATES)),
            ("write_date", ">=", yesterday_from),
            ("write_date", "<=", yesterday_to),
        ])
        return {
            "in_progress_user_ids": in_progress_user_ids,
            "has_in_progress_work": bool(in_progress),
            "completed_today": completed_today,
            "completed_yesterday": completed_yesterday,
            "overdue_task_count": 0,
        }
