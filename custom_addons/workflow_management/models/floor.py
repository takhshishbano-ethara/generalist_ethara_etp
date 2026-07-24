from odoo import models, fields, api


class Floor(models.Model):
    _name = "workflow.floor"
    _description = "Office Floor"
    _order = "sequence, name"

    name = fields.Char(string="Floor Name", required=True)
    floor_code = fields.Char(
        string="Floor Code",
        required=True,
        help="Unique identifier like 'ground', 'first', 'second'",
    )
    sequence = fields.Integer(string="Sequence", default=10)
    active = fields.Boolean(default=True)

    seat_ids = fields.One2many("workflow.seat", "floor_id", string="Seats")

    total_seats = fields.Integer(
        string="Total Seats", compute="_compute_seat_counts", store=True,
    )
    occupied_seats = fields.Integer(
        string="Occupied Seats", compute="_compute_seat_counts", store=True,
    )
    available_seats = fields.Integer(
        string="Available Seats", compute="_compute_seat_counts", store=True,
    )
    reserved_seats = fields.Integer(
        string="Reserved Seats", compute="_compute_seat_counts", store=True,
    )

    _floor_code_unique = models.Constraint(
        "UNIQUE(floor_code)",
        "Floor code must be unique.",
    )

    @api.depends("seat_ids.status")
    def _compute_seat_counts(self):
        for floor in self:
            seats = floor.seat_ids
            floor.total_seats = len(seats)
            floor.occupied_seats = len(seats.filtered(lambda s: s.status == "occupied"))
            floor.available_seats = len(seats.filtered(lambda s: s.status == "available"))
            floor.reserved_seats = len(seats.filtered(lambda s: s.status == "reserved"))

    def name_get(self):
        return [(f.id, f"{f.name} ({f.floor_code})") for f in self]
