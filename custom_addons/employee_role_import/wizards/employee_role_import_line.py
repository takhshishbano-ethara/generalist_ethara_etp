import re

from odoo import api, fields, models

from ..models.hr_employee import ROLE_LEVEL, ROLE_SELECTION

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmployeeRoleImportLine(models.TransientModel):
    _name = "employee.role.import.line"
    _description = "Employee Role Import - Preview Row"
    _order = "sequence, id"

    wizard_id = fields.Many2one(
        "employee.role.import.wizard",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(string="Row #", readonly=True)
    name = fields.Char(string="Name", required=True)
    employee_code = fields.Char(string="Employee ID")
    email = fields.Char(string="Email", required=True)
    role = fields.Selection(
        selection=ROLE_SELECTION,
        string="Role",
        help="Role applied for this row. Override per-row if needed.",
    )
    manager_email = fields.Char(
        string="Reports To (email)",
        help="Email of the manager from the CSV. Used to resolve the Reports To employee.",
    )
    parent_id = fields.Many2one(
        "hr.employee",
        string="Reports To",
        help="Manager assigned on import. Editable - falls back to looking up manager_email if empty.",
    )
    status = fields.Selection(
        [
            ("ready", "Ready"),
            ("issue", "Issue"),
            ("exists", "Already Exists"),
            ("exists_archived", "Archived (Deleted)"),
        ],
        compute="_compute_status",
        store=True,
        string="Status",
    )
    existing_employee_id = fields.Many2one(
        "hr.employee", compute="_compute_status", store=True, string="Existing Employee"
    )
    existing_user_id = fields.Many2one(
        "res.users", compute="_compute_status", store=True, string="Existing User"
    )
    existing_archived = fields.Boolean(
        compute="_compute_status", store=True, string="Existing Record Archived"
    )

    @api.depends("name", "email", "role", "employee_code", "parent_id", "parent_id.role")
    def _compute_status(self):
        Employee = self.env["hr.employee"].sudo().with_context(active_test=False)
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        for line in self:
            issues = []
            if not (line.name or "").strip():
                issues.append("missing name")
            email = (line.email or "").strip().lower()
            if not email:
                issues.append("missing email")
            elif not EMAIL_RE.match(email):
                issues.append("invalid email")
            if not line.role:
                issues.append("missing role")
            if line.role and line.parent_id and line.parent_id.role:
                emp_level = ROLE_LEVEL.get(line.role, 0)
                parent_level = ROLE_LEVEL.get(line.parent_id.role, 0)
                if parent_level <= emp_level:
                    issues.append(
                        f"manager '{line.parent_id.name}' has role "
                        f"'{line.parent_id.role}' which is not senior to '{line.role}'"
                    )

            code = (line.employee_code or "").strip()
            existing_emp = Employee.search([("employee_code", "=ilike", code)], limit=1) if code else Employee.browse()
            if not existing_emp and email:
                existing_emp = Employee.search([("work_email", "=ilike", email)], limit=1)
            existing_user = Users.search([("login", "=ilike", email)], limit=1) if email else Users.browse()
            archived = bool(
                (existing_emp and not existing_emp.active)
                or (existing_user and not existing_user.active)
            )
            line.existing_employee_id = existing_emp.id if existing_emp else False
            line.existing_user_id = existing_user.id if existing_user else False
            line.existing_archived = archived

            if issues:
                line.status = "issue"
            elif existing_emp or existing_user:
                line.status = "exists_archived" if archived else "exists"
            else:
                line.status = "ready"
