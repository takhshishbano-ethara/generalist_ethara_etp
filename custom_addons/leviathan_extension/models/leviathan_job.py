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

DONE_STATES = ("done", "submitted")

# Both shippable verdicts count as approved; not_shippable counts as rework.
APPROVED_VERDICTS = ("shippable", "fixes")
REWORK_VERDICTS = ("not_shippable",)


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


class LeviathanJob(models.Model):
    _inherit = "leviathan.job"

    @api.model
    def _performance_scope_domain(self):
        """Role-scoped search domain on ``leviathan.job``.

        CTO/TPM see every job, PL/QR see the jobs of the taskers reporting
        to them (and their own), taskers and everyone else see only their
        own jobs. Mirrors ``_job_scope_domain`` in ``controllers/main.py``.
        """
        env = self.env
        user = env.user
        if _user_has_role(env, FULL_ACCESS_ROLE_XMLIDS):
            return []
        field = None
        if _user_has_role(env, PL_ROLE_XMLIDS):
            field = "task_forge_pl_id"
        elif _user_has_role(env, QR_ROLE_XMLIDS):
            field = "task_forge_qr_id"
        if not field:
            return [("user_id", "=", user.id)]
        Employee = env["hr.employee"].sudo()
        employees = Employee.search([("user_id", "=", user.id)])
        team = Employee.search([(field, "in", employees.ids)])
        user_ids = (team | employees).mapped("user_id").ids
        return [("user_id", "in", user_ids)]

    @api.model
    def get_performance_metrics(self):
        """Return role-scoped performance metrics for the calling user.

        Approval % and rework % are shares of QC-reviewed jobs (jobs with a
        ``qc_verdict`` set); task-done counts ``done``/``submitted`` jobs;
        average handling time is the mean ``duration_seconds`` over jobs
        with a positive duration. Scope follows
        :meth:`_performance_scope_domain`.
        """
        domain = self._performance_scope_domain()
        Job = self.sudo()

        total_task_count = Job.search_count(domain)
        task_done = Job.search_count(
            domain + [("state", "in", list(DONE_STATES))]
        )

        qc_reviewed_count = Job.search_count(
            domain + [("qc_verdict", "!=", False)]
        )
        approved_count = Job.search_count(
            domain + [("qc_verdict", "in", list(APPROVED_VERDICTS))]
        )
        rework_count = Job.search_count(
            domain + [("qc_verdict", "in", list(REWORK_VERDICTS))]
        )

        aht_domain = domain + [("duration_seconds", ">", 0)]
        aht_measured_count = Job.search_count(aht_domain)
        rows = Job.read_group(
            aht_domain, fields=["duration_seconds:avg"], groupby=[]
        )
        avg_seconds = (rows[0].get("duration_seconds") if rows else 0.0) or 0.0

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
