import logging
from datetime import date, datetime

from odoo import api, fields, models

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

DONE_STATES = ("done", "submitted")
IN_PROGRESS_STATES = ("extracting", "generating", "scoring")

APPROVED_VERDICTS = ("shippable", "fixes")
REWORK_VERDICTS = ("not_shippable",)
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


class VegetaJob(models.Model):
    _inherit = "vegeta.job"

    llm_qc_cost_usd = fields.Float(
        string="LLM QC Cost (USD)",
        digits=(12, 6),
        default=0.0,
        help="USD cost of LLM-based QC for this job. Populated by the QC pipeline.",
    )

    @api.model
    def get_member_task_stats(self, user_id):
        """Per-member "tasks done" + average duration for the shared project
        Team tab (``/api/v2/project_team_member_list``).

        vegeta.job is the Vegeta project's ``connected_table``, so a member's
        "tasks done" is the count of their jobs in a terminal/shipped state —
        NOT a ``task.forge.log`` count (which is empty for Vegeta). The shared
        Team endpoint calls this hook when the project's connected backend
        exposes it; backends without it keep the legacy task.forge.log count.
        """
        if not user_id:
            return {"done": 0, "avg_seconds": 0}
        Job = self.sudo()
        done_domain = [
            ("user_id", "=", user_id),
            ("state", "in", list(DONE_STATES)),
        ]
        done = Job.search_count(done_domain)
        groups = Job._read_group(
            done_domain + [("duration_seconds", ">", 0)],
            [],
            ["duration_seconds:avg"],
        )
        avg_seconds = (groups[0][0] if groups else 0.0) or 0.0
        return {"done": done, "avg_seconds": int(avg_seconds)}

    @api.model
    def _performance_scope_domain(self):
        """Role-scoped search domain on ``vegeta.job``.

        CTO/TPM see every job; PL/QC see the jobs of the taskers on the
        projects they lead or review (and their own); taskers and everyone
        else see only their own jobs. Team membership is read from
        ``project.project`` records, consistent with the vegeta_extension
        dashboard endpoints.
        """
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
        """Return role-scoped performance metrics for the calling user.

        Approval % and rework % are shares of QC-decided jobs (verdict in
        shippable/fixes/not_shippable); task-done counts ``done``/``submitted``
        jobs; average handling time is the mean ``duration_seconds`` over
        jobs with a positive duration. Scope follows
        :meth:`_performance_scope_domain`.
        """
        domain = self._performance_scope_domain()
        Job = self.sudo()

        total_task_count = Job.search_count(domain)
        task_done = Job.search_count(
            domain + [("state", "in", list(DONE_STATES))]
        )

        qc_reviewed_count = Job.search_count(
            domain + [("qc_verdict", "in", list(DECIDED_VERDICTS))]
        )
        approved_count = Job.search_count(
            domain + [("qc_verdict", "in", list(APPROVED_VERDICTS))]
        )
        rework_count = Job.search_count(
            domain + [("qc_verdict", "in", list(REWORK_VERDICTS))]
        )

        aht_domain = domain + [("duration_seconds", ">", 0)]
        aht_measured_count = Job.search_count(aht_domain)
        groups = Job._read_group(aht_domain, [], ["duration_seconds:avg"])
        avg_seconds = (groups[0][0] if groups else 0.0) or 0.0

        return {
            "total_task_count": total_task_count,
            "task_done": task_done,
            "approval_percentage": _pct(approved_count, qc_reviewed_count),
            "rework_percentage": _pct(rework_count, qc_reviewed_count),
            "avg_handling_time_seconds": round(avg_seconds, 2),
            "avg_handling_time_minutes": round(avg_seconds / 60.0, 2),
            "approved_count": approved_count,
            "rework_count": rework_count,
            "qc_reviewed_count": qc_reviewed_count,
            "aht_measured_count": aht_measured_count,
        }

    @api.model
    def get_list_metrics(self):
        """Lightweight subset of :meth:`get_performance_metrics` for the
        project-list endpoint: only the total / done / aht-measured counts it
        actually displays. Skips the QC aggregation the list throws away, so it
        costs 3 ``search_count`` queries instead of 6 + a ``_read_group``."""
        domain = self._performance_scope_domain()
        Job = self.sudo()
        return {
            "total_task_count": Job.search_count(domain),
            "task_done": Job.search_count(domain + [("state", "in", list(DONE_STATES))]),
            "aht_measured_count": Job.search_count(domain + [("duration_seconds", ">", 0)]),
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
            ("llm_qc_cost_usd", ">", 0),
            ("completed_at", ">=", dt_from),
            ("completed_at", "<=", dt_to),
        ]
        rows = self.sudo().read_group(
            domain=domain,
            fields=["completed_at", "llm_qc_cost_usd:sum"],
            groupby=["completed_at:day"],
            lazy=False,
        )
        out = []
        for row in rows:
            raw = row.get("completed_at:day") or row.get("completed_at")
            cost = float(row.get("llm_qc_cost_usd") or 0.0)
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
            overdue_task_count:   0 (no overdue state on vegeta.job)
        """
        Job = self.sudo()
        in_progress = Job.search([("state", "in", list(IN_PROGRESS_STATES))])
        in_progress_user_ids = list({
            uid for uid in in_progress.mapped("user_id").ids if uid
        })
        completed_today = Job.search_count([
            ("state", "in", list(DONE_STATES)),
            ("completed_at", ">=", today_from),
            ("completed_at", "<=", today_to),
        ])
        completed_yesterday = Job.search_count([
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
