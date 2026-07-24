from odoo import models, fields


class SeatHistory(models.Model):
    _name = "workflow.seat.history"
    _description = "Seat Assignment History"
    _order = "create_date desc"

    seat_id = fields.Many2one(
        "workflow.seat", string="Seat", required=True, ondelete="cascade",
    )
    employee_id = fields.Many2one(
        "hr.employee", string="Employee", required=True, ondelete="cascade",
    )
    floor_id = fields.Many2one(
        "workflow.floor", string="Floor", ondelete="set null",
    )
    action = fields.Selection(
        [
            ("assign", "Assigned"),
            ("release", "Released"),
            ("transfer", "Transferred"),
        ],
        string="Action",
        required=True,
    )
    notes = fields.Text(string="Notes")
