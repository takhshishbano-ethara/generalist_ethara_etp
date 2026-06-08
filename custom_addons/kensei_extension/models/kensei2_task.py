from odoo import api, models


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

DONE_STATES = ("Submitted",)
APPROVED_VERDICTS = ("passed",)
REWORK_VERDICTS = ("failed",)
DECIDED_VERDICTS = APPROVED_VERDICTS + REWORK_VERDICTS


def _role_ids(env, xmlids):
    ids = []
    for xmlid in xmlids:
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            ids.append(record.id)
    return ids


def _user_has_role(env, xmlids):
    role = env.user.user_role
    if not role:
        return False
    return role.id in _role_ids(env, xmlids)


def _pct(part, whole):
    if not whole:
        return 0.0
    return round((part / whole) * 100, 2)


class Kensei2Task(models.Model):
    _inherit = "kensei2.kensei2"

    @api.model
    def _performance_scope_domain(self):
        env = self.env
        user = env.user
        if _user_has_role(env, FULL_ACCESS_ROLE_XMLIDS):
            return []
        employee = env["hr.employee"].sudo().search(
            [("user_id", "=", user.id)], limit=1
        )
        if _user_has_role(env, PL_ROLE_XMLIDS):
            projects = env["project.project"].sudo().search(
                [("project_lead", "in", employee.ids)]
            )
            tasker_user_ids = projects.mapped("project_tasker").mapped("user_id").ids
            user_ids = list(set(tasker_user_ids) | {user.id})
            return [("user_id", "in", user_ids)]
        if _user_has_role(env, QR_ROLE_XMLIDS):
            projects = env["project.project"].sudo().search(
                [("project_qc_reviewer", "in", employee.ids)]
            )
            tasker_user_ids = projects.mapped("project_tasker").mapped("user_id").ids
            user_ids = list(set(tasker_user_ids) | {user.id})
            return [("user_id", "in", user_ids)]
        return [("user_id", "=", user.id)]

    @api.model
    def get_performance_metrics(self):
        domain = self._performance_scope_domain()
        total = self.sudo().search_count(domain)
        done_domain = domain + [("task_status", "in", list(DONE_STATES))]
        done = self.sudo().search_count(done_domain)
        approved_domain = domain + [("qc_status", "in", list(APPROVED_VERDICTS))]
        approved = self.sudo().search_count(approved_domain)
        rework_domain = domain + [("qc_status", "in", list(REWORK_VERDICTS))]
        rework = self.sudo().search_count(rework_domain)
        decided_domain = domain + [("qc_status", "in", list(DECIDED_VERDICTS))]
        decided = self.sudo().search_count(decided_domain)
        return {
            "total_task_count": total,
            "task_done": done,
            "approval_percentage": _pct(approved, decided),
            "rework_percentage": _pct(rework, decided),
            "approved_count": approved,
            "rework_count": rework,
            "qc_reviewed_count": decided,
        }
