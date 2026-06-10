import logging
from datetime import date, datetime

from odoo import api, models

_logger = logging.getLogger(__name__)

MANAGER_GROUP = "fenrir.group_fenrir_manager"
TASKER_GROUP = "fenrir.group_fenrir_tasker"

DONE_STATES = ("completed", "approved")
IN_PROGRESS_STATES = ("pending_review",)
FAILED_STATES = ("rejected", "cancelled")

APPROVED_VERDICTS = ("approved", "completed")
REWORK_VERDICTS = ("rejected", "cancelled")
DECIDED_VERDICTS = APPROVED_VERDICTS + REWORK_VERDICTS


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


def _task_cost(task):
    """Sum of accepted seller offers' price_paid_usd for a task."""
    return sum(
        (o.price_paid_usd or o.final_payment_amount or 0.0)
        for o in task.seller_offer_ids.filtered(lambda o: o.accepted == "yes")
    )


class FenrirTask(models.Model):
    _inherit = "fenrir.task"

    @api.model
    def _performance_scope_domain(self):
        env = self.env
        user = env.user
        if user.has_group(MANAGER_GROUP):
            return []
        return [("lead_user_id", "=", user.id)]

    @api.model
    def get_performance_metrics(self):
        domain = self._performance_scope_domain()
        Task = self.sudo()

        total_task_count = Task.search_count(domain)
        task_done = Task.search_count(
            domain + [("status", "in", list(DONE_STATES))]
        )

        reviewed_count = Task.search_count(
            domain + [("status", "in", list(DECIDED_VERDICTS))]
        )
        approved_count = Task.search_count(
            domain + [("status", "in", list(APPROVED_VERDICTS))]
        )
        rework_count = Task.search_count(
            domain + [("status", "in", list(REWORK_VERDICTS))]
        )

        aht_domain = domain + [("estimated_completion_time_hours", ">", 0)]
        aht_measured_count = Task.search_count(aht_domain)
        aht_groups = Task._read_group(
            aht_domain, [], ["estimated_completion_time_hours:avg"]
        )
        avg_hours = (aht_groups[0][0] if aht_groups else 0.0) or 0.0
        avg_seconds = avg_hours * 3600.0

        cost_tasks = Task.search(domain)
        per_task_costs = [(_task_cost(t)) for t in cost_tasks if _task_cost(t) > 0]
        cost_measured_count = len(per_task_costs)
        total_cost = sum(per_task_costs)
        avg_cost = (total_cost / cost_measured_count) if cost_measured_count else 0.0

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
    def get_tasks_completed_timeseries(self, dt_from, dt_to):
        domain = [
            ("status", "in", list(DONE_STATES)),
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
    def get_main_dashboard_metrics(self, today_from, today_to, yesterday_from, yesterday_to):
        """Founder-summary metrics for the heterogeneous-table aggregator.

        Every key is always present so the controller can aggregate uniformly
        across modules. fenrir.task uses ``lead_user_id`` for the worker and
        ``write_date`` for completion timestamps (no dedicated decided_at
        field on the model).

            in_progress_user_ids: res.users IDs currently in pending_review
            has_in_progress_work: any pending-review row exists right now
            completed_today:      tasks in DONE_STATES updated today
            completed_yesterday:  tasks in DONE_STATES updated yesterday
            overdue_task_count:   0 (no overdue state on fenrir.task)
        """
        Task = self.sudo()
        in_progress = Task.search([("status", "in", list(IN_PROGRESS_STATES))])
        in_progress_user_ids = list({
            uid for uid in in_progress.mapped("lead_user_id").ids if uid
        })
        completed_today = Task.search_count([
            ("status", "in", list(DONE_STATES)),
            ("write_date", ">=", today_from),
            ("write_date", "<=", today_to),
        ])
        completed_yesterday = Task.search_count([
            ("status", "in", list(DONE_STATES)),
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
