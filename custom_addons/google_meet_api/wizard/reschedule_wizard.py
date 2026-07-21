from datetime import timedelta

from odoo import _, fields, models
from odoo.exceptions import ValidationError


class GoogleMeetRescheduleWizard(models.TransientModel):
    _name = "google.meet.reschedule.wizard"
    _description = "Reschedule Interview"

    event_id = fields.Many2one("calendar.event", required=True, ondelete="cascade")
    current_start = fields.Datetime(related="event_id.start", readonly=True)
    new_scheduled_at = fields.Datetime(required=True, default=fields.Datetime.now)
    new_duration_minutes = fields.Integer(default=60, required=True)
    notify_attendees = fields.Boolean(default=True)
    reason = fields.Text(help="Optional reason to include in the notification.")

    def action_reschedule(self):
        self.ensure_one()
        if self.new_duration_minutes <= 0:
            raise ValidationError(_("Duration must be positive."))

        old_start = self.event_id.start
        stop = self.new_scheduled_at + timedelta(minutes=self.new_duration_minutes)
        self.event_id.write({
            "start": self.new_scheduled_at,
            "stop": stop,
            "duration": self.new_duration_minutes / 60.0,
        })

        if self.notify_attendees and self.event_id.attendee_ids:
            body = (
                f"<p><strong>Interview rescheduled.</strong></p>"
                f"<p>Old: {old_start} → New: {self.new_scheduled_at}</p>"
            )
            if self.reason:
                body += f"<p><strong>Reason:</strong> {self.reason}</p>"
            self.event_id.message_post(
                body=body,
                partner_ids=self.event_id.partner_ids.ids,
                subtype_xmlid="mail.mt_comment",
                force_send=True,
            )
            self.event_id.attendee_ids.with_context(
                no_mail_to_attendees=False,
                mail_notify_force_send=True,
            )._send_invitation_emails()

        return {
            "type": "ir.actions.act_window",
            "res_model": "calendar.event",
            "view_mode": "form",
            "res_id": self.event_id.id,
            "target": "current",
        }
