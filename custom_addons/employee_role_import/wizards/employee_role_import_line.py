import re

from odoo import api, fields, models

from ..models.hr_employee import ROLE_HIERARCHY_FIELDS, ROLE_LEVEL, ROLE_SELECTION

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
    def _selection_assignable_role(self):
        return self.env["hr.employee"]._assignable_role_selection()

    role = fields.Selection(
        selection="_selection_assignable_role",
        string="Role",
        help="Role applied for this row. Override per-row if needed.",
    )
    job_title = fields.Char(string="Job Title")
    assigned_ql_id = fields.Many2one(
        "hr.employee",
        string="Assigned QL",
        domain="[('role', 'in', ('ql', 'qr'))]",
        help="Quality Lead this person reports to in the Task Forge hierarchy.",
    )
    assigned_pl_id = fields.Many2one(
        "hr.employee",
        string="Assigned PL",
        domain="[('role', '=', 'pl')]",
        help="Project Lead this person reports to in the Task Forge hierarchy.",
    )
    assigned_tpm_id = fields.Many2one(
        "hr.employee",
        string="Assigned TPM",
        domain="[('role', '=', 'tpm')]",
        help="TPM this person reports to in the Task Forge hierarchy.",
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
            ("exists", "Exists"),
            ("invalid", "Invalid"),
        ],
        compute="_compute_status",
        store=True,
        string="Status",
    )
    has_issues = fields.Boolean(
        compute="_compute_status",
        store=True,
        string="Has Issues",
    )
    issue_text = fields.Text(
        compute="_compute_status",
        store=True,
        string="Issue Details",
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

    @api.onchange("email", "employee_code", "name", "role")
    def _onchange_revalidate(self):
        self._compute_status()

    @api.onchange("role")
    def _onchange_role_clear_hierarchy(self):
        for line in self:
            applicable = ROLE_HIERARCHY_FIELDS.get(line.role or "", ())
            if "ql" not in applicable:
                line.assigned_ql_id = False
            if "pl" not in applicable:
                line.assigned_pl_id = False
            if "tpm" not in applicable:
                line.assigned_tpm_id = False

    @api.depends("name", "email", "role", "employee_code", "parent_id", "parent_id.role",
                 "assigned_ql_id", "assigned_pl_id", "assigned_tpm_id")
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
            elif line.role not in self.env["hr.employee"]._current_user_assignable_roles():
                issues.append(
                    "role '%s' is not assignable by you" % line.role
                )
            if line.role and line.parent_id and line.parent_id.role:
                emp_level = ROLE_LEVEL.get(line.role, 0)
                parent_level = ROLE_LEVEL.get(line.parent_id.role, 0)
                if parent_level <= emp_level:
                    issues.append(
                        f"manager '{line.parent_id.name}' has role "
                        f"'{line.parent_id.role}' which is not senior to '{line.role}'"
                    )

            applicable = ROLE_HIERARCHY_FIELDS.get(line.role or "", ())
            if line.assigned_ql_id and "ql" not in applicable:
                issues.append("assigned QL not allowed for role '%s'" % line.role)
            if line.assigned_pl_id and "pl" not in applicable:
                issues.append("assigned PL not allowed for role '%s'" % line.role)
            if line.assigned_tpm_id and "tpm" not in applicable:
                issues.append("assigned TPM not allowed for role '%s'" % line.role)

            required = {"tasker": ("assigned_ql_id", "QL/QR"),
                        "qr": ("assigned_pl_id", "PL"),
                        "ql": ("assigned_pl_id", "PL"),
                        "pl": ("assigned_tpm_id", "TPM")}.get(line.role or "")
            if required and not line[required[0]]:
                issues.append("a %s is required for role '%s'" % (required[1], line.role))

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

            # A row with issues (e.g. a QL with no PL) must NOT look importable:
            # mark it invalid so the preview shows it in red and it can't be
            # mistaken for "ready".
            if issues:
                line.status = "invalid"
            else:
                line.status = "exists" if (existing_emp or existing_user) else "ready"
            line.has_issues = bool(issues)
            line.issue_text = "; ".join(issues) if issues else False
