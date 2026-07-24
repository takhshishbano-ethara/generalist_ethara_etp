import json
from odoo import models, fields, api


class AuditLog(models.Model):
    _name = "workflow.audit.log"
    _description = "Audit Log"
    _order = "create_date desc"

    event = fields.Char(string="Event", required=True, index=True)
    category = fields.Selection(
        [
            ("seat_management", "Seat Management"),
            ("seat_transfer", "Seat Transfer"),
            ("room_booking", "Room Booking"),
            ("employee_management", "Employee Management"),
            ("attendance", "Attendance"),
            ("system", "System"),
        ],
        string="Category",
        required=True,
        index=True,
    )
    status = fields.Selection(
        [
            ("success", "Success"),
            ("failure", "Failure"),
        ],
        string="Status",
        default="success",
    )

    user_id = fields.Many2one("res.users", string="User", ondelete="set null")
    user_name = fields.Char(string="User Name")
    details_json = fields.Text(string="Details (JSON)")
    summary = fields.Char(string="Summary", compute="_compute_summary", store=True)

    @api.depends("event", "user_name", "details_json")
    def _compute_summary(self):
        for rec in self:
            rec.summary = f"[{rec.event}] by {rec.user_name or 'System'}"

    @api.model
    def log_event(self, event, category, details=None, status="success"):
        """Create an audit log entry.

        Call from anywhere:
            self.env['workflow.audit.log'].log_event(
                event='seat_assigned',
                category='seat_management',
                details={'seat_code': 'F2-A-012', 'employee': 'John'},
            )
        """
        user = self.env.user
        self.sudo().create({
            "event": event,
            "category": category,
            "status": status,
            "user_id": user.id if user else False,
            "user_name": user.name if user else "System",
            "details_json": json.dumps(details or {}, default=str),
        })
