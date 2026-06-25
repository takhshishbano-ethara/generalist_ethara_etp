from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    # Employee ID exposed on the user form/list as a related field over the
    # linked hr.employee. Intentionally NOT stored: this keeps the user form
    # working without adding a column to res_users (a stored column here broke
    # production when the module had not been upgraded yet). hr.employee
    # remains the single source of truth for the code.
    employee_code = fields.Char(
        related="employee_id.employee_code",
        string="Employee ID",
        readonly=True,
        store=False,
        help="Employee ID from the linked employee record.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._etp_link_employee()
        users.employee_ids._etp_sync_job_title()
        return users

    def write(self, vals):
        res = super().write(vals)
        ctx_ok = not self.env.context.get("etp_skip_eu_sync")
        if ctx_ok and ("login" in vals or "email" in vals):
            self._etp_link_employee()
        if ctx_ok and ("group_ids" in vals or "groups_id" in vals):
            self.employee_ids._etp_sync_job_title()
        return res

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
