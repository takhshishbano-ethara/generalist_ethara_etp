from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResUsers(models.Model):
    _inherit = "res.users"

    # Employee ID for the person behind this login. Stored and editable so it
    # can be assigned straight from the Settings > Users form (even before an
    # hr.employee is linked) and kept unique across every user and employee.
    # When a matching employee is linked, the value is mirrored to it.
    employee_code = fields.Char(
        string="Employee ID",
        copy=False,
        index=True,
        help="Unique Employee ID. Must not be reused by any other user or "
        "employee. Mirrored to the linked employee record.",
    )

    _EMP_CODE_CHECK_FIELDS = frozenset({"employee_code", "login", "email"})

    # Not @api.constrains: that fires on every flush (including module-upgrade
    # recomputes when columns are added/backfilled), which can blow up the
    # upgrade itself. Uniqueness only matters on user-initiated create/write,
    # so we drive this from those overrides instead.
    def _check_employee_code_unique(self):
        for user in self:
            code = (user.employee_code or "").strip()
            if not code:
                continue
            dup_user = self.with_context(active_test=False).sudo().search(
                [("id", "!=", user.id), ("employee_code", "=ilike", code)],
                limit=1,
            )
            if dup_user:
                raise ValidationError(_(
                    "Employee ID '%(code)s' is already used by another user "
                    "(%(name)s). Employee IDs must be unique."
                ) % {"code": code, "name": dup_user.name or dup_user.login})
            # An employee that is THIS user (linked, or matched by email and not
            # yet linked) legitimately shares the code; anything else clashes.
            idents = {(user.login or "").strip().lower(),
                      (user.email or "").strip().lower()} - {""}
            emps = self.env["hr.employee"].with_context(
                active_test=False
            ).sudo().search([("employee_code", "=ilike", code)])
            clash = emps.filtered(
                lambda e: e.user_id.id != user.id
                and (e.work_email or "").strip().lower() not in idents
            )
            if clash:
                raise ValidationError(_(
                    "Employee ID '%(code)s' is already used by employee "
                    "%(name)s. Employee IDs must be unique across users and "
                    "employees."
                ) % {"code": code, "name": clash[0].name})

    @staticmethod
    def _etp_normalize_employee_code(vals):
        """Force the Employee ID upper-cased and trimmed, matching hr.employee
        so the same code is stored identically on both sides."""
        code = vals.get("employee_code")
        if isinstance(code, str):
            vals["employee_code"] = code.strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._etp_normalize_employee_code(vals)
        users = super().create(vals_list)
        users._etp_link_employee()
        users._etp_sync_employee_code()
        users.employee_ids._etp_sync_job_title()
        users._check_employee_code_unique()
        return users

    def write(self, vals):
        self._etp_normalize_employee_code(vals)
        res = super().write(vals)
        ctx_ok = not self.env.context.get("etp_skip_eu_sync")
        if ctx_ok and ("login" in vals or "email" in vals):
            self._etp_link_employee()
        if ctx_ok and (
            "employee_code" in vals or "login" in vals or "email" in vals
        ):
            self._etp_sync_employee_code()
        if ctx_ok and ("group_ids" in vals or "groups_id" in vals):
            self.employee_ids._etp_sync_job_title()
        if self._EMP_CODE_CHECK_FIELDS.intersection(vals):
            self._check_employee_code_unique()
        return res

    def _etp_sync_employee_code(self):
        """Mirror the Employee ID between the user and its linked employee. The
        user form is the entry point, so a code typed there flows to the
        employee; if the user has none but the employee does, adopt it."""
        if self.env.context.get("etp_skip_eu_sync"):
            return
        for user in self:
            emp = (user.employee_ids[:1] or user.employee_id)
            if not emp:
                continue
            code = (user.employee_code or "").strip()
            emp_code = (emp.employee_code or "").strip()
            if code and emp_code != code:
                emp.with_context(etp_skip_eu_sync=True).employee_code = code
            elif not code and emp_code:
                user.with_context(etp_skip_eu_sync=True).employee_code = emp_code

    def _etp_link_employee(self):
        """Link each (internal) user to the hr.employee whose work_email
        matches, when that employee has no user yet. Never creates an
        employee; never steals one already linked elsewhere. Idempotent."""
        if self.env.context.get("etp_skip_eu_sync"):
            return
        Emp = self.env["hr.employee"].sudo()
        for user in self:
            if user.share:
                continue
            login = (user.login or "").strip()
            if not login:
                continue
            emp = Emp.search(
                ["&", ("user_id", "in", (False, user.id)),
                 "|", ("work_email", "=ilike", login),
                 ("work_email", "=ilike", user.email or login)],
                limit=2,
            )
            emp = emp.filtered(lambda e: not e.user_id)[:1]
            if not emp:
                continue
            emp.with_context(etp_skip_eu_sync=True).user_id = user.id
