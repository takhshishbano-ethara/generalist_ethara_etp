# -*- coding: utf-8 -*-
import random

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class BerserkerMassAssignAccess(models.TransientModel):
    _name = "berserker.mass.assign.access"
    _description = "Mass Assign Berserker User Access"

    employee_ids = fields.Many2many(
        "hr.employee",
        string="Employees",
        required=True,
    )
    employee_count = fields.Integer(
        string="Employees Selected",
        compute="_compute_employee_count",
    )

    @api.depends("employee_ids")
    def _compute_employee_count(self):
        for wizard in self:
            wizard.employee_count = len(wizard.employee_ids)

    def action_grant_access(self):
        if not self.employee_ids:
            raise UserError(_("No employees selected."))

        user_group = self.env.ref("berserker.group_berserker_user")
        granted = 0
        for emp in self.employee_ids:
            user = emp.user_id
            if not user:
                continue
            if not user.has_group("berserker.group_berserker_user"):
                user.sudo().write({"groups_id": [(4, user_group.id)]})
                granted += 1

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Access Granted"),
                "message": _("Berserker User access granted to %d employee(s).")
                % granted,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }


class BerserkerMassAssignTasks(models.TransientModel):
    _name = "berserker.mass.assign"
    _description = "Mass Assign Berserker Tasks"

    mode = fields.Selection(
        [
            ("specific", "Assign all to one employee"),
            ("distribute", "Distribute randomly among employees"),
        ],
        string="Assignment Mode",
        default="distribute",
        required=True,
    )
    employee_id = fields.Many2one(
        "hr.employee",
        string="Employee",
    )
    tasks_per_employee = fields.Integer(
        string="Tasks per Employee",
        default=0,
        help="Number of tasks per employee. 0 = distribute all evenly.",
    )
    task_count = fields.Integer(
        string="Tasks Selected",
        compute="_compute_task_count",
    )
    eligible_employee_count = fields.Integer(
        string="Eligible Employees",
        compute="_compute_eligible_count",
    )

    @api.depends_context("active_ids")
    def _compute_task_count(self):
        for wizard in self:
            wizard.task_count = len(self.env.context.get("active_ids", []))

    def _compute_eligible_count(self):
        user_group = self.env.ref("berserker.group_berserker_user")
        employees = self.env["hr.employee"].search(
            [
                ("user_id", "!=", False),
                ("user_id.groups_id", "in", user_group.id),
            ]
        )
        for wizard in self:
            wizard.eligible_employee_count = len(employees)

    def action_assign(self):
        active_ids = self.env.context.get("active_ids", [])
        if not active_ids:
            raise UserError(_("No tasks selected."))

        tasks = self.env["berserker"].browse(active_ids)

        if self.mode == "specific":
            if not self.employee_id:
                raise UserError(_("Please select an employee."))
            tasks.write({"employee_id": self.employee_id.id})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Tasks Assigned"),
                    "message": _("%d task(s) assigned to %s.")
                    % (len(active_ids), self.employee_id.name),
                    "type": "success",
                    "sticky": False,
                    "next": {"type": "ir.actions.act_window_close"},
                },
            }

        # Distribute mode — random round-robin among all berserker users
        user_group = self.env.ref("berserker.group_berserker_user")
        employees = self.env["hr.employee"].search(
            [
                ("user_id", "!=", False),
                ("user_id.groups_id", "in", user_group.id),
            ]
        )
        if not employees:
            raise UserError(
                _("No employees have Berserker User access. Grant access first.")
            )

        emp_list = list(employees)
        random.shuffle(emp_list)

        task_list = list(tasks)
        random.shuffle(task_list)

        if self.tasks_per_employee > 0:
            # Fixed count per employee
            idx = 0
            assigned = 0
            for emp in emp_list:
                chunk = task_list[idx : idx + self.tasks_per_employee]
                if not chunk:
                    break
                for t in chunk:
                    t.write({"employee_id": emp.id})
                    assigned += 1
                idx += self.tasks_per_employee
        else:
            # Even distribution — round-robin
            assigned = 0
            for i, task in enumerate(task_list):
                emp = emp_list[i % len(emp_list)]
                task.write({"employee_id": emp.id})
                assigned += 1

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Tasks Distributed"),
                "message": _("%d task(s) distributed among %d employee(s).")
                % (assigned, len(emp_list)),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
