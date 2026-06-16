from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

ROLE_GROUP_MAP = {
    "tasker": ("etp_user_roles.group_tasker", "Tasker"),
    "ql": ("etp_user_roles.group_quality_lead", "Quality Lead"),
    "qr": ("etp_user_roles.group_quality_reviewer", "Quality Reviewer"),
    "pl": ("etp_user_roles.group_project_lead", "Project Lead"),
    "dm": ("etp_user_roles.group_delivery_manager", "Delivery Manager"),
    "tpm": ("etp_user_roles.group_tpm", "TPM"),
    "cto": ("etp_user_roles.group_cto", "CTO"),
    "hr_admin": ("etp_user_roles.group_hr_admin", "HR Admin"),
    "it_admin": ("etp_user_roles.group_it_admin", "IT Admin"),
}

ROLE_SELECTION = [(k, v[1]) for k, v in ROLE_GROUP_MAP.items()]

# Senior-first lookup order so a user holding both TPM and PL reports as TPM.
ROLE_PRIORITY = ["cto", "tpm", "dm", "pl", "ql", "qr", "tasker", "hr_admin", "it_admin"]

ROLE_LEVEL = {
    "tasker": 1,
    "qr": 2,
    "ql": 3,
    "pl": 4,
    "dm": 4,
    "tpm": 5,
    "cto": 6,
    "hr_admin": 6,
    "it_admin": 6,
}

ROLE_DEFAULT_PARENT_ROLE = {
    "tasker": "qr",
    "qr": "ql",
    "ql": "pl",
    "pl": "tpm",
    "dm": "tpm",
    "tpm": "cto",
}


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    employee_code = fields.Char(
        string="Employee ID",
        index=True,
        copy=False,
        help="External employee identifier supplied at import time "
        "(maps to the `employee_id` column in the CSV).",
    )

    @api.constrains("employee_code")
    def _check_employee_code_unique(self):
        for emp in self:
            code = (emp.employee_code or "").strip()
            if not code:
                continue
            dup = self.with_context(active_test=False).sudo().search(
                [("id", "!=", emp.id), ("employee_code", "=ilike", code)],
                limit=1,
            )
            if dup:
                raise ValidationError(_(
                    "Employee ID '%(code)s' already exists "
                    "(used by %(name)s%(archived)s). "
                    "Pick a different ID or update that record instead."
                ) % {
                    "code": code,
                    "name": dup.name or _("an archived record"),
                    "archived": "" if dup.active else _(" — archived"),
                })

    @api.constrains("work_email")
    def _check_work_email_unique(self):
        for emp in self:
            email = (emp.work_email or "").strip().lower()
            if not email:
                continue
            dup = self.with_context(active_test=False).sudo().search(
                [("id", "!=", emp.id), ("work_email", "=ilike", email)],
                limit=1,
            )
            if dup:
                raise ValidationError(_(
                    "Work Email '%(email)s' already exists "
                    "(used by %(name)s%(archived)s). "
                    "Pick a different email or update that record instead."
                ) % {
                    "email": email,
                    "name": dup.name or _("an archived record"),
                    "archived": "" if dup.active else _(" — archived"),
                })

    def _collect_linked_users_to_unlink(self):
        protected_ids = {self.env.user.id}
        for xml_id in ("base.user_admin", "base.user_root"):
            protected = self.env.ref(xml_id, raise_if_not_found=False)
            if protected:
                protected_ids.add(protected.id)
        users = self.env["res.users"]
        for emp in self.with_context(active_test=False):
            if emp.user_id and emp.user_id.id not in protected_ids:
                users |= emp.user_id
        return users

    def unlink(self):
        users_to_unlink = self._collect_linked_users_to_unlink()
        result = super().unlink()
        if users_to_unlink:
            users_to_unlink.exists().sudo().with_context(active_test=False).unlink()
        return result

    def action_delete_with_user(self):
        emp_count = len(self)
        users_to_unlink = self._collect_linked_users_to_unlink()
        self.unlink()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Records removed"),
                "message": _(
                    "%(emp)s employee(s) and %(usr)s login user(s) were permanently deleted."
                ) % {"emp": emp_count, "usr": len(users_to_unlink)},
                "type": "success",
                "sticky": False,
            },
        }

    role = fields.Selection(
        selection=ROLE_SELECTION,
        string="Role",
        compute="_compute_role",
        inverse="_inverse_role",
        store=True,
        help="ETP role of the linked user. Changing this rewrites the user's "
        "group membership (the previous role is removed, the new one is added).",
    )

    @api.depends("user_id")
    def _compute_role(self):
        priority_groups = [
            (key, self.env.ref(ROLE_GROUP_MAP[key][0], raise_if_not_found=False))
            for key in ROLE_PRIORITY
        ]
        for emp in self:
            current = False
            if emp.user_id:
                user_group_ids = set(emp.user_id.group_ids.ids)
                for key, group in priority_groups:
                    if group and group.id in user_group_ids:
                        current = key
                        break
            emp.role = current

    @api.constrains("role", "parent_id")
    def _check_reports_to_role(self):
        for emp in self:
            if not emp.parent_id or not emp.role:
                continue
            parent_role = emp.parent_id.role
            if not parent_role:
                continue
            emp_level = ROLE_LEVEL.get(emp.role, 0)
            parent_level = ROLE_LEVEL.get(parent_role, 0)
            if parent_level <= emp_level:
                raise ValidationError(
                    _(
                        "Invalid Reports To for %(emp)s: a %(emp_role)s cannot "
                        "report to %(parent)s (%(parent_role)s). The manager's "
                        "role must be senior to the employee's role.",
                        emp=emp.name,
                        emp_role=dict(ROLE_SELECTION).get(emp.role, emp.role),
                        parent=emp.parent_id.name,
                        parent_role=dict(ROLE_SELECTION).get(parent_role, parent_role),
                    )
                )

    def _inverse_role(self):
        all_role_groups = {
            key: self.env.ref(xml_id, raise_if_not_found=False)
            for key, (xml_id, _label) in ROLE_GROUP_MAP.items()
        }
        internal = self.env.ref("base.group_user", raise_if_not_found=False)
        for emp in self:
            if not emp.user_id:
                if emp.role:
                    raise UserError(
                        _("Cannot set a role on an employee without a linked user.")
                    )
                continue
            commands = []
            for key, group in all_role_groups.items():
                if not group:
                    continue
                if key == emp.role:
                    commands.append((4, group.id))
                elif group.id in emp.user_id.group_ids.ids:
                    commands.append((3, group.id))
            if emp.role and internal and internal.id not in emp.user_id.group_ids.ids:
                commands.append((4, internal.id))
            if commands:
                emp.user_id.sudo().write({"group_ids": commands})
