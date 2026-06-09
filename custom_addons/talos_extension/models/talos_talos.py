import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

FULL_ACCESS_GROUPS = (
    "talos.group_talos_admin",
    "etp_user_roles.group_quality_lead",
)

PL_GROUPS = (
    "etp_user_roles.group_project_lead",
)

TASKER_GROUPS = (
    "etp_user_roles.group_tasker",
)

DONE_STATUSES = ("Submitted",)
APPROVED_VERDICTS = ("passed",)
REWORK_VERDICTS = ("failed",)
DECIDED_VERDICTS = APPROVED_VERDICTS + REWORK_VERDICTS


def _user_in_any(env, xmlids):
    user = env.user
    for xmlid in xmlids:
        if user.has_group(xmlid):
            return True
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
        if _user_in_any(env, FULL_ACCESS_GROUPS):
            return []
        if _user_in_any(env, PL_GROUPS):
            return []
        return [("user_id", "=", user.id)]

    @api.model
    def get_performance_metrics(self):
        domain = self._performance_scope_domain()
        Talos = self.sudo()

        total_task_count = Talos.search_count(domain)
        task_done = Talos.search_count(
            domain + [("task_status", "in", list(DONE_STATUSES))]
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

        return {
            "total_task_count": total_task_count,
            "task_done": task_done,
            "approval_percentage": _pct(approved_count, reviewed_count),
            "rework_percentage": _pct(rework_count, reviewed_count),
            "approved_count": approved_count,
            "rework_count": rework_count,
            "reviewed_count": reviewed_count,
        }
