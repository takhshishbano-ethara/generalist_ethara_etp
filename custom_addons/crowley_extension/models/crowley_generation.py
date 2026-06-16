import logging
from datetime import date, datetime

from odoo import api, models

_logger = logging.getLogger(__name__)

FULL_ACCESS_ROLE_XMLIDS = (
    "api_auth_gateway.role_cto_technical",
    "api_auth_gateway.role_tpm_technical",
)

PL_ROLE_XMLIDS = (
    "api_auth_gateway.role_pl_technical",
    "api_auth_gateway.role_pl_stem",
    "api_auth_gateway.role_pl_non_stem",
)

QR_ROLE_XMLIDS = (
    "api_auth_gateway.role_qc_technical",
    "api_auth_gateway.role_qc_stem",
    "api_auth_gateway.role_qc_non_stem",
)

DONE_STATES = ("done",)
IN_PROGRESS_STATES = ("queued", "submitting", "processing", "downloading")

APPROVED_VERDICTS = ("approved",)
REWORK_VERDICTS = ("rejected",)
DECIDED_VERDICTS = APPROVED_VERDICTS + REWORK_VERDICTS


def _role_ids(env, xmlids):
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
    return role.id in _role_ids(env, xmlids)


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


class CrowleyGeneration(models.Model):
    _inherit = "crowley.generation"

    @api.model
    def _performance_scope_domain(self):
        env = self.env
        user = env.user
        if _user_has_role(env, FULL_ACCESS_ROLE_XMLIDS):
            return []
        field = None
        if _user_has_role(env, PL_ROLE_XMLIDS):
            field = "project_lead"
        elif _user_has_role(env, QR_ROLE_XMLIDS):
            field = "project_qc_reviewer"
        if not field:
            return [("user_id", "=", user.id)]
        employee = env["hr.employee"].sudo().search(
            [("user_id", "=", user.id)], limit=1
        )
        projects = env["project.project"].sudo().search(
            [(field, "in", employee.ids)]
        )
        taskers = projects.mapped("project_tasker")
        user_ids = (taskers.mapped("user_id") | user).ids
        return [("user_id", "in", user_ids)]

    @api.model
    def get_performance_metrics(self):
        domain = self._performance_scope_domain()
        Gen = self.sudo()

        total_task_count = Gen.search_count(domain)
        task_done = Gen.search_count(
            domain + [("state", "in", list(DONE_STATES))]
        )

        reviewed_count = Gen.search_count(
            domain + [("review_state", "in", list(DECIDED_VERDICTS))]
        )
        approved_count = Gen.search_count(
            domain + [("review_state", "in", list(APPROVED_VERDICTS))]
        )
        rework_count = Gen.search_count(
            domain + [("review_state", "in", list(REWORK_VERDICTS))]
        )

        aht_domain = domain + [("duration_seconds", ">", 0)]
        aht_measured_count = Gen.search_count(aht_domain)
        aht_groups = Gen._read_group(aht_domain, [], ["duration_seconds:avg"])
        avg_seconds = (aht_groups[0][0] if aht_groups else 0.0) or 0.0

        cost_domain = domain + [("cost_usd", ">", 0)]
        cost_measured_count = Gen.search_count(cost_domain)
        cost_groups = Gen._read_group(
            cost_domain, [], ["cost_usd:sum", "cost_usd:avg"]
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
        away, so it costs 3 ``search_count`` queries instead of 8+."""
        domain = self._performance_scope_domain()
        Gen = self.sudo()
        return {
            "total_task_count": Gen.search_count(domain),
            "task_done": Gen.search_count(domain + [("state", "in", list(DONE_STATES))]),
            "aht_measured_count": Gen.search_count(domain + [("duration_seconds", ">", 0)]),
        }

    @api.model
    def get_tasks_completed_timeseries(self, dt_from, dt_to):
        domain = [
            ("state", "in", list(DONE_STATES)),
            ("completed_at", ">=", dt_from),
            ("completed_at", "<=", dt_to),
        ]
        rows = self.sudo().read_group(
            domain=domain,
            fields=["completed_at"],
            groupby=["completed_at:day"],
            lazy=False,
        )
        out = []
        for row in rows:
            raw = row.get("completed_at:day") or row.get("completed_at")
            count = int(row.get("__count") or 0)
            if not raw or not count:
                continue
            if isinstance(raw, datetime):
                iso = raw.date().isoformat()
            elif isinstance(raw, date):
                iso = raw.isoformat()
            else:
                parsed = None
                for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y"):
                    try:
                        parsed = datetime.strptime(str(raw), fmt).date()
                        break
                    except ValueError:
                        continue
                if not parsed:
                    continue
                iso = parsed.isoformat()
            out.append((iso, count))
        return out

    @api.model
    def get_daily_burn_timeseries(self, dt_from, dt_to):
        domain = [
            ("cost_usd", ">", 0),
            ("completed_at", ">=", dt_from),
            ("completed_at", "<=", dt_to),
        ]
        rows = self.sudo().read_group(
            domain=domain,
            fields=["completed_at", "cost_usd:sum"],
            groupby=["completed_at:day"],
            lazy=False,
        )
        out = []
        for row in rows:
            raw = row.get("completed_at:day") or row.get("completed_at")
            cost = float(row.get("cost_usd") or 0.0)
            if not raw or cost <= 0:
                continue
            if isinstance(raw, datetime):
                iso = raw.date().isoformat()
            elif isinstance(raw, date):
                iso = raw.isoformat()
            else:
                parsed = None
                for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y"):
                    try:
                        parsed = datetime.strptime(str(raw), fmt).date()
                        break
                    except ValueError:
                        continue
                if not parsed:
                    continue
                iso = parsed.isoformat()
            out.append((iso, cost))
        return out

    @api.model
    def get_main_dashboard_metrics(self, today_from, today_to, yesterday_from, yesterday_to):
        """Return raw founder-summary metrics from this task table.

        Every key is always present so the controller can aggregate
        uniformly across heterogeneous connected tables:

            in_progress_user_ids: res.users IDs currently running tasks
            has_in_progress_work: any in-progress row exists right now
            completed_today:      done rows with completion ts today
            completed_yesterday:  done rows with completion ts yesterday
            overdue_task_count:   0 (no overdue state on crowley.generation)
        """
        Gen = self.sudo()
        in_progress = Gen.search([("state", "in", list(IN_PROGRESS_STATES))])
        in_progress_user_ids = list({
            uid for uid in in_progress.mapped("user_id").ids if uid
        })
        completed_today = Gen.search_count([
            ("state", "in", list(DONE_STATES)),
            ("completed_at", ">=", today_from),
            ("completed_at", "<=", today_to),
        ])
        completed_yesterday = Gen.search_count([
            ("state", "in", list(DONE_STATES)),
            ("completed_at", ">=", yesterday_from),
            ("completed_at", "<=", yesterday_to),
        ])
        return {
            "in_progress_user_ids": in_progress_user_ids,
            "has_in_progress_work": bool(in_progress),
            "completed_today": completed_today,
            "completed_yesterday": completed_yesterday,
            "overdue_task_count": 0,
        }
