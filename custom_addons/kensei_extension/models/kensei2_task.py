from odoo import api, fields, models


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

    state = fields.Char(
        string="State",
        compute="_compute_state",
        search="_search_state",
        help=(
            "Cross-extension alias for task_status. Generic controllers in "
            "benchmark addons (gohan/vegeta/leviathan/employee_extension) "
            "filter every connected task model by a field literally named "
            "'state' with values like 'submitted' / 'done' / 'in_progress'. "
            "kensei2.kensei2 uses task_status instead; this computed field "
            "lets those generic controllers work against Kensei without "
            "schema changes inside kensei2."
        ),
    )

    @api.depends("task_status")
    def _compute_state(self):
        for rec in self:
            rec.state = (
                "submitted" if rec.task_status == "Submitted" else "in_progress"
            )

    duration_seconds = fields.Float(
        string="Duration (seconds)",
        compute="_compute_duration_seconds",
        help=(
            "Cross-extension shim for kensei2.kensei2. Generic dashboard "
            "controllers in task_forge_bridge aggregate per-task duration "
            "via `mapped('duration_seconds')`. Kensei2 does not track "
            "per-task timing the way gohan does, so this always returns "
            "0.0 \u2014 'no duration data' is the correct semantic. Without "
            "this field, callers raise KeyError when the project's "
            "connected_table points at kensei2.kensei2."
        ),
    )

    @api.depends()
    def _compute_duration_seconds(self):
        for rec in self:
            rec.duration_seconds = 0.0

    quality_score = fields.Float(
        string="Quality Score",
        compute="_compute_quality_score",
        help=(
            "Cross-extension shim for kensei2.kensei2. "
            "task_forge_bridge.dashboard_controllers.get_tasker_dashboard_list "
            "aggregates per-day quality via "
            "`read_group(fields=['quality_score'], groupby=['write_date:day'])`. "
            "Kensei2 dropped the score/quality concept (per its README) — "
            "QC outcome is captured as `qc_status` (passed/failed/pending) "
            "instead. This always returns 0.0 so the read_group does not "
            "raise KeyError when the connected_table points at kensei2."
        ),
    )

    @api.depends()
    def _compute_quality_score(self):
        for rec in self:
            rec.quality_score = 0.0

    def _search_state(self, operator, value):
        submitted_aliases = ("submitted", "done", "completed")
        in_progress_aliases = (
            "in_progress",
            "draft",
            "pending",
            "notsubmitted",
            "not_submitted",
        )
        values = value if isinstance(value, (list, tuple)) else [value]
        matches_submitted = any(
            str(v).lower() in submitted_aliases for v in values
        )
        matches_in_progress = any(
            str(v).lower() in in_progress_aliases for v in values
        )
        if operator in ("=", "in"):
            targets = []
            if matches_submitted:
                targets.append("Submitted")
            if matches_in_progress:
                targets.append("NotSubmitted")
            return (
                [("task_status", "in", targets)]
                if targets
                else [("id", "=", False)]
            )
        if operator in ("!=", "not in"):
            excluded = []
            if matches_submitted:
                excluded.append("Submitted")
            if matches_in_progress:
                excluded.append("NotSubmitted")
            return (
                [("task_status", "not in", excluded)]
                if excluded
                else [("id", "!=", False)]
            )
        return [("id", "!=", False)]

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
