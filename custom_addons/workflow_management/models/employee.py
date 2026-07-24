from odoo import models, fields, api


class Employee(models.Model):
    _inherit = "hr.employee"

    project_category = fields.Selection(
        [
            ("stem", "STEM"),
            ("non_stem", "Non-STEM"),
        ],
        string="Project Category",
    )
    current_project = fields.Char(string="Current Project")
    platform = fields.Selection(
        [
            ("windows", "Windows"),
            ("linux", "Linux"),
            ("mac", "Mac"),
        ],
        string="Platform",
    )
    tasking_status = fields.Selection(
        [
            ("active", "Active"),
            ("inactive", "Inactive"),
            ("on_leave", "On Leave"),
        ],
        string="Tasking Status",
        default="active",
    )
    allocation_status = fields.Selection(
        [
            ("fully_allocated", "Fully Allocated"),
            ("partially_allocated", "Partially Allocated"),
            ("unallocated", "Unallocated"),
        ],
        string="Allocation Status",
        default="unallocated",
    )
    main_location = fields.Char(string="Main Location")
    current_location = fields.Char(string="Current Location")
    project_color = fields.Char(string="Project Color", default="#3b82f6")

    current_seat_id = fields.Many2one(
        "workflow.seat",
        string="Current Seat",
        ondelete="set null",
    )
    seat_history_ids = fields.One2many(
        "workflow.seat.history",
        "employee_id",
        string="Seat History",
    )

    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        for emp in employees:
            self.env["workflow.audit.log"].sudo().log_event(
                event="employee_created",
                category="employee_management",
                details={"employee_id": emp.id, "name": emp.name},
            )
        return employees
