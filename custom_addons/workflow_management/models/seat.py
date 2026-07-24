from odoo import models, fields, api
from odoo.exceptions import UserError


class Seat(models.Model):
    _name = "workflow.seat"
    _description = "Office Seat"
    _order = "floor_id, zone, row, number"
    _inherit = ["mail.thread"]

    seat_code = fields.Char(
        string="Seat ID",
        required=True,
        help="Unique seat identifier, e.g. F2-A-012",
    )
    name = fields.Char(string="Label", compute="_compute_name", store=True)
    floor_id = fields.Many2one(
        "workflow.floor",
        string="Floor",
        required=True,
        ondelete="cascade",
        tracking=True,
    )
    row = fields.Char(string="Row", required=True)
    number = fields.Integer(string="Seat Number", required=True)
    zone = fields.Char(string="Zone", help="e.g. Left-Wing, Central, Right-Wing")

    status = fields.Selection(
        [
            ("available", "Available"),
            ("occupied", "Occupied"),
            ("reserved", "Reserved"),
            ("maintenance", "Maintenance"),
        ],
        string="Status",
        default="available",
        required=True,
        tracking=True,
    )

    position_x = fields.Float(string="Position X", help="Canvas X coordinate")
    position_y = fields.Float(string="Position Y", help="Canvas Y coordinate")

    employee_id = fields.Many2one(
        "hr.employee",
        string="Assigned Employee",
        ondelete="set null",
        tracking=True,
    )

    history_ids = fields.One2many(
        "workflow.seat.history", "seat_id", string="Assignment History",
    )

    _seat_code_unique = models.Constraint(
        "UNIQUE(seat_code)",
        "Seat code must be unique.",
    )

    @api.depends("seat_code", "zone")
    def _compute_name(self):
        for seat in self:
            seat.name = f"{seat.seat_code} ({seat.zone})" if seat.zone else seat.seat_code

    def action_assign(self, employee, notes=""):
        """Assign this seat to an employee. Releases employee's old seat if any."""
        self.ensure_one()
        if self.status not in ("available", "reserved"):
            raise UserError(
                f"Seat {self.seat_code} is {self.status} and cannot be assigned."
            )

        # Release employee's current seat if they have one
        old_seat = self.env["workflow.seat"].search(
            [("employee_id", "=", employee.id), ("id", "!=", self.id)], limit=1,
        )
        if old_seat:
            old_seat.action_release()

        self.write({"employee_id": employee.id, "status": "occupied"})

        # Update employee's current_seat_id
        employee.sudo().write({"current_seat_id": self.id})

        # Create history record
        self.env["workflow.seat.history"].create({
            "seat_id": self.id,
            "employee_id": employee.id,
            "floor_id": self.floor_id.id,
            "action": "assign",
            "notes": notes or f"Assigned to {employee.name}",
        })

        # Audit log
        self.env["workflow.audit.log"].log_event(
            event="seat_assigned",
            category="seat_management",
            details={
                "seat_code": self.seat_code,
                "employee_name": employee.name,
                "employee_id": employee.id,
                "floor": self.floor_id.name,
            },
        )

    def action_release(self):
        """Release this seat, making it available."""
        self.ensure_one()
        old_employee = self.employee_id
        self.write({"employee_id": False, "status": "available"})

        if old_employee:
            old_employee.sudo().write({"current_seat_id": False})
            self.env["workflow.seat.history"].create({
                "seat_id": self.id,
                "employee_id": old_employee.id,
                "floor_id": self.floor_id.id,
                "action": "release",
                "notes": f"Released from {old_employee.name}",
            })

        self.env["workflow.audit.log"].log_event(
            event="seat_released",
            category="seat_management",
            details={
                "seat_code": self.seat_code,
                "old_employee": old_employee.name if old_employee else "",
            },
        )

    def action_set_maintenance(self):
        """Put seat into maintenance mode."""
        self.ensure_one()
        if self.employee_id:
            raise UserError("Release the seat before marking it for maintenance.")
        self.write({"status": "maintenance"})

    def action_set_available(self):
        """Return seat from maintenance to available."""
        self.ensure_one()
        if self.status != "maintenance":
            raise UserError("Only maintenance seats can be set to available this way.")
        self.write({"status": "available"})

    def action_reserve(self):
        """Reserve a seat."""
        self.ensure_one()
        if self.status != "available":
            raise UserError("Only available seats can be reserved.")
        self.write({"status": "reserved"})
