from odoo import _, fields, models


class GoogleMeetCancelWizard(models.TransientModel):
    _name = "google.meet.cancel.wizard"
    _description = "Cancel Interview"

    event_id = fields.Many2one("calendar.event", required=True, ondelete="cascade")
    event_name = fields.Char(related="event_id.name", readonly=True)
    event_start = fields.Datetime(related="event_id.start", readonly=True)
    reason = fields.Selection([
        ("candidate_unavailable", "Candidate unavailable"),
        ("interviewer_unavailable", "Interviewer unavailable"),
        ("candidate_dropped", "Candidate dropped out"),
        ("role_closed", "Role closed / no longer hiring"),
        ("rescheduled_elsewhere", "Rescheduled via different channel"),
        ("other", "Other"),
    ], required=True, default="other")
    comment = fields.Text()
    notify_attendees = fields.Boolean(default=True)

    def action_cancel(self):
        self.ensure_one()
        if self.notify_attendees and self.event_id.attendee_ids:
            reason_label = dict(self._fields["reason"].selection).get(self.reason)
            body = (
                f"<p><strong>Interview cancelled.</strong></p>"
                f"<p><strong>Reason:</strong> {reason_label}</p>"
            )
            if self.comment:
                body += f"<p>{self.comment}</p>"
            self.event_id.message_post(
                body=body,
                partner_ids=self.event_id.partner_ids.ids,
                subtype_xmlid="mail.mt_comment",
                force_send=True,
            )
        self.event_id.unlink()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Interview cancelled"),
                "message": _("The interview was cancelled and attendees notified."),
                "type": "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
