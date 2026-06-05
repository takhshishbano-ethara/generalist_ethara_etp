import logging

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

DONE_STATES = ("exported",)

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


class VideoEditorProject(models.Model):
    _inherit = "video.editor.project"

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
            return [("assigned_to", "=", user.id)]
        employee = env["hr.employee"].sudo().search(
            [("user_id", "=", user.id)], limit=1
        )
        projects = env["project.project"].sudo().search(
            [(field, "in", employee.ids)]
        )
        taskers = projects.mapped("project_tasker")
        user_ids = (taskers.mapped("user_id") | user).ids
        return [("assigned_to", "in", user_ids)]

    @api.model
    def get_performance_metrics(self):
        domain = self._performance_scope_domain()
        Gen = self.sudo()

        total_task_count = Gen.search_count(domain)
        task_done = Gen.search_count(
            domain + [("state", "in", list(DONE_STATES))]
        )

        reviewed_count = Gen.search_count(
            domain + [("review_status", "in", list(DECIDED_VERDICTS))]
        )
        approved_count = Gen.search_count(
            domain + [("review_status", "in", list(APPROVED_VERDICTS))]
        )
        rework_count = Gen.search_count(
            domain + [("review_status", "in", list(REWORK_VERDICTS))]
        )

        aht_domain = domain + [("duration_seconds", ">", 0)]
        aht_measured_count = Gen.search_count(aht_domain)
        aht_groups = Gen._read_group(aht_domain, [], ["duration_seconds:avg"])
        avg_seconds = (aht_groups[0][0] if aht_groups else 0.0) or 0.0

        cost_domain = domain + [("llm_qc_cost_usd", ">", 0)]
        cost_measured_count = Gen.search_count(cost_domain)
        cost_groups = Gen._read_group(
            cost_domain, [], ["llm_qc_cost_usd:sum", "llm_qc_cost_usd:avg"]
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
