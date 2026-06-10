import logging
from datetime import date, datetime

from odoo import api, models

_logger = logging.getLogger(__name__)

FULL_ACCESS_GROUP_XMLIDS = (
    "etp_user_roles.group_cto",
    "etp_user_roles.group_tpm",
    "etp_user_roles.group_founder",
)

PL_GROUP_XMLIDS = (
    "etp_user_roles.group_project_lead",
    "etp_user_roles.group_delivery_manager",
)

QR_GROUP_XMLIDS = (
    "etp_user_roles.group_quality_reviewer",
    "etp_user_roles.group_quality_lead",
)

TASKER_GROUP_XMLIDS = (
    "etp_user_roles.group_tasker",
)

DONE_STATES = ("Submitted",)
IN_PROGRESS_GOLDEN = ("generating",)
IN_PROGRESS_AUTO = ("queued", "processing")

APPROVED_VERDICTS = ("passed",)
REWORK_VERDICTS = ("failed",)
DECIDED_VERDICTS = APPROVED_VERDICTS + REWORK_VERDICTS

TOKEN_FIELDS = (
    "claude_input_tokens", "claude_output_tokens",
    "glm_input_tokens", "glm_output_tokens",
    "oneP_input_tokens", "oneP_output_tokens",
    "onePA_input_tokens", "onePA_output_tokens",
    "onePB_input_tokens", "onePB_output_tokens",
    "onePC_input_tokens", "onePC_output_tokens",
    "onePD_input_tokens", "onePD_output_tokens",
    "bedrock_input_tokens", "bedrock_output_tokens",
    "traj_qc_input_tokens", "traj_qc_output_tokens",
    "taskdesc_input_tokens", "taskdesc_output_tokens",
    "golden_input_tokens", "golden_output_tokens",
    "kimi_eval_input_tokens", "kimi_eval_output_tokens",
)


def _total_tokens(rec):
    return sum(int(getattr(rec, f, 0) or 0) for f in TOKEN_FIELDS)


def _user_has_any_group(env, xmlids):
    for xmlid in xmlids:
        try:
            if env.user.has_group(xmlid):
                return True
        except Exception:
            continue
    return False


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 2)


class TalosTalos(models.Model):
    _inherit = "talos.talos"

    @api.model
    def _performance_scope_domain(self):
        env = self.env
        user = env.user
        if _user_has_any_group(env, FULL_ACCESS_GROUP_XMLIDS):
            return []
        if _user_has_any_group(env, PL_GROUP_XMLIDS):
            return []
        if _user_has_any_group(env, QR_GROUP_XMLIDS):
            return []
        return [("user_id", "=", user.id)]

    @api.model
    def get_performance_metrics(self):
        domain = self._performance_scope_domain()
        Talos = self.sudo()

        total_task_count = Talos.search_count(domain)
        task_done = Talos.search_count(
            domain + [("task_status", "in", list(DONE_STATES))]
        )

        reviewed_count = Talos.search_count(
            domain + [("qc_status", "in", list(DECIDED_VERDICTS))]
        )
        approved_count = Talos.search_count(
            domain + [("qc_status", "in", list(APPROVED_VERDICTS))]
        )
        rework_count = Talos.search_count(
            domain + [("qc_status", "in", list(REWORK_VERDICTS))]
        )

        records = Talos.search(domain)
        token_values = [_total_tokens(r) for r in records]
        non_zero = [v for v in token_values if v > 0]
        total_tokens = sum(token_values)
        cost_measured_count = len(non_zero)
        avg_tokens = (sum(non_zero) / len(non_zero)) if non_zero else 0.0

        return {
            "total_task_count": total_task_count,
            "task_done": task_done,
            "approval_percentage": _pct(approved_count, reviewed_count),
            "rework_percentage": _pct(rework_count, reviewed_count),
            "avg_handling_time_seconds": 0.0,
            "avg_handling_time_minutes": 0.0,
            "approved_count": approved_count,
            "rework_count": rework_count,
            "reviewed_count": reviewed_count,
            "aht_measured_count": 0,
            "total_cost_usd": round(total_tokens, 6),
            "average_cost_usd": round(avg_tokens, 6),
            "cost_measured_count": cost_measured_count,
        }

    @api.model
    def get_tasks_completed_timeseries(self, dt_from, dt_to):
        domain = [
            ("task_status", "in", list(DONE_STATES)),
            ("write_date", ">=", dt_from),
            ("write_date", "<=", dt_to),
        ]
        records = self.sudo().search(domain)
        counts = {}
        for rec in records:
            when = rec.write_date
            if not when:
                continue
            day = when.date().isoformat()
            counts[day] = counts.get(day, 0) + 1
        return sorted([(iso, count) for iso, count in counts.items()])

    @api.model
    def get_daily_burn_timeseries(self, dt_from, dt_to):
        domain = [
            ("create_date", ">=", dt_from),
            ("create_date", "<=", dt_to),
        ]
        records = self.sudo().search(domain)
        totals = {}
        for rec in records:
            when = rec.create_date
            if not when:
                continue
            tokens = _total_tokens(rec)
            if tokens <= 0:
                continue
            day = when.date().isoformat()
            totals[day] = totals.get(day, 0.0) + float(tokens)
        return sorted([(iso, cost) for iso, cost in totals.items()])

    @api.model
    def get_main_dashboard_metrics(self, today_from, today_to, yesterday_from, yesterday_to):
        Talos = self.sudo()
        in_progress = Talos.search([
            "|",
            ("golden_status", "in", list(IN_PROGRESS_GOLDEN)),
            ("auto_process_status", "in", list(IN_PROGRESS_AUTO)),
        ])
        in_progress_user_ids = list({
            uid for uid in in_progress.mapped("user_id").ids if uid
        })
        completed_today = Talos.search_count([
            ("task_status", "in", list(DONE_STATES)),
            ("write_date", ">=", today_from),
            ("write_date", "<=", today_to),
        ])
        completed_yesterday = Talos.search_count([
            ("task_status", "in", list(DONE_STATES)),
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
