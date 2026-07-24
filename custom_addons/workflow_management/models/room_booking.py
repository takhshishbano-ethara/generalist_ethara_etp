from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError


class RoomBooking(models.Model):
    _name = "workflow.room.booking"
    _description = "Room / Conference Booking"
    _order = "booking_start desc"
    _inherit = ["mail.thread"]

    name = fields.Char(string="Room Name", required=True, tracking=True)
    room_code = fields.Char(
        string="Room Code", required=True,
        help="Unique room identifier, e.g. CONF-01",
    )
    floor_id = fields.Many2one("workflow.floor", string="Floor", ondelete="set null")

    organizer_id = fields.Many2one(
        "res.users", string="Organizer",
        default=lambda self: self.env.user,
        required=True,
    )
    organizer_email = fields.Char(
        string="Organizer Email",
        related="organizer_id.email",
        store=True,
        readonly=True,
    )
    reason = fields.Text(string="Booking Reason")

    booking_start = fields.Datetime(string="Start Time", required=True, tracking=True)
    booking_end = fields.Datetime(string="End Time", required=True, tracking=True)
    duration_minutes = fields.Integer(
        string="Duration (minutes)",
        compute="_compute_duration",
        store=True,
    )

    status = fields.Selection(
        [
            ("confirmed", "Confirmed"),
            ("cancelled", "Cancelled"),
            ("completed", "Completed"),
        ],
        string="Status",
        default="confirmed",
        required=True,
        tracking=True,
    )

    @api.depends("booking_start", "booking_end")
    def _compute_duration(self):
        for booking in self:
            if booking.booking_start and booking.booking_end:
                delta = booking.booking_end - booking.booking_start
                booking.duration_minutes = int(delta.total_seconds() / 60)
            else:
                booking.duration_minutes = 0

    @api.constrains("booking_start", "booking_end")
    def _check_dates(self):
        for booking in self:
            if booking.booking_start and booking.booking_end:
                if booking.booking_end <= booking.booking_start:
                    raise ValidationError("End time must be after start time.")

    @api.constrains("room_code", "booking_start", "booking_end", "status")
    def _check_overlap(self):
        """Prevent double-booking the same room."""
        for booking in self:
            if booking.status == "cancelled":
                continue
            overlapping = self.search([
                ("id", "!=", booking.id),
                ("room_code", "=", booking.room_code),
                ("status", "=", "confirmed"),
                ("booking_start", "<", booking.booking_end),
                ("booking_end", ">", booking.booking_start),
            ])
            if overlapping:
                raise ValidationError(
                    f"Room {booking.room_code} is already booked from "
                    f"{overlapping[0].booking_start} to {overlapping[0].booking_end}."
                )

    def action_cancel(self):
        """Cancel the booking."""
        for booking in self:
            if booking.status != "confirmed":
                raise UserError("Only confirmed bookings can be cancelled.")
            booking.write({"status": "cancelled"})
            self.env["workflow.audit.log"].log_event(
                event="booking_cancelled",
                category="room_booking",
                details={
                    "room_code": booking.room_code,
                    "room_name": booking.name,
                    "organizer": booking.organizer_id.name,
                },
            )

    def action_complete(self):
        """Mark booking as completed."""
        for booking in self:
            if booking.status != "confirmed":
                raise UserError("Only confirmed bookings can be completed.")
            booking.write({"status": "completed"})

    @api.model_create_multi
    def create(self, vals_list):
        bookings = super().create(vals_list)
        for booking in bookings:
            self.env["workflow.audit.log"].sudo().log_event(
                event="booking_created",
                category="room_booking",
                details={
                    "room_code": booking.room_code,
                    "room_name": booking.name,
                    "organizer": booking.organizer_id.name,
                    "start": str(booking.booking_start),
                    "end": str(booking.booking_end),
                },
            )
        return bookings
