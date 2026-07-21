import base64
import logging
from datetime import timedelta
from urllib.parse import urlparse

import requests

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class GoogleMeetScheduleWizard(models.TransientModel):
    _name = "google.meet.schedule.wizard"
    _description = "Schedule Google Meet Interview"

    title = fields.Char(required=True)
    scheduled_at = fields.Datetime(required=True, default=fields.Datetime.now)
    duration_minutes = fields.Integer(default=60, required=True)
    mode = fields.Selection(
        [("online", "Online (Google Meet)"), ("offline", "Offline")],
        default="online", required=True,
    )
    location = fields.Char()
    candidate_id = fields.Many2one(
        "hr.applicant",
        string="Candidate (Interviewee)",
        help="The person being interviewed. Their resume and applied job will be attached.",
    )
    job_id = fields.Many2one(
        "hr.job",
        string="Applied for (Job Position)",
        help="Job the candidate applied for. JD is included in the invite.",
    )
    resume_url = fields.Char(related="candidate_id.resume_url", readonly=True, string="Resume")
    attendee_ids = fields.Many2many(
        "res.partner",
        string="Interviewers",
        help="The people who will conduct the interview.",
    )
    notes = fields.Text()
    reminder_minutes = fields.Integer(default=15)
    job_link = fields.Char(string="Extra Link (optional)")
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "gm_schedule_wizard_attachment_rel",
        "wizard_id", "attachment_id",
        string="Extra Attachments",
    )

    @api.onchange("candidate_id")
    def _onchange_candidate_id(self):
        if not self.candidate_id:
            return
        c = self.candidate_id
        if not self.title:
            self.title = f"Interview - {c.partner_name or c.name or 'Candidate'}"
        if c.job_id and not self.job_id:
            self.job_id = c.job_id

        partner = c.partner_id
        if not partner and c.email_from:
            Partner = self.env["res.partner"]
            partner = Partner.search([("email", "=ilike", c.email_from)], limit=1)
            if not partner:
                partner = Partner.create({
                    "name": c.partner_name or c.email_from,
                    "email": c.email_from,
                })
        if partner:
            self.attendee_ids = [(4, partner.id)]

    def action_schedule(self):
        self.ensure_one()
        if self.duration_minutes <= 0:
            raise ValidationError(_("Duration must be positive."))
        if self.mode == "offline" and not (self.location or "").strip():
            raise ValidationError(_("Location is required for offline interviews."))

        partner_ids = list(self.attendee_ids.ids)
        partner_ids = list(dict.fromkeys(partner_ids))

        description_parts = []
        if self.notes:
            description_parts.append(f"<p>{self.notes}</p>")
        if self.job_id:
            description_parts.append(
                f"<p><strong>Job Position:</strong> {self.job_id.name}</p>"
            )
            jd = self.job_id.description
            if jd:
                description_parts.append(f"<p><strong>Job Description:</strong></p>{jd}")
        if self.candidate_id and self.candidate_id.resume_url:
            description_parts.append(
                f'<p><strong>Resume:</strong> '
                f'<a href="{self.candidate_id.resume_url}" target="_blank">'
                f'Download candidate resume</a></p>'
            )
        if self.job_link:
            description_parts.append(
                f'<p><strong>Link:</strong> '
                f'<a href="{self.job_link}" target="_blank">{self.job_link}</a></p>'
            )

        vals = {
            "name": self.title,
            "start": self.scheduled_at,
            "stop": self.scheduled_at + timedelta(minutes=self.duration_minutes),
            "duration": self.duration_minutes / 60.0,
            "location": self.location or False,
            "description": "".join(description_parts) or False,
            "is_google_meet": self.mode == "online",
        }
        if partner_ids:
            vals["partner_ids"] = [(6, 0, partner_ids)]

        if self.reminder_minutes and self.reminder_minutes > 0:
            Alarm = self.env["calendar.alarm"]
            alarm = Alarm.search([
                ("alarm_type", "=", "email"),
                ("duration", "=", self.reminder_minutes),
                ("interval", "=", "minutes"),
            ], limit=1) or Alarm.create({
                "name": f"{self.reminder_minutes} minutes before (email)",
                "alarm_type": "email",
                "duration": self.reminder_minutes,
                "interval": "minutes",
            })
            vals["alarm_ids"] = [(6, 0, [alarm.id])]

        event = self.env["calendar.event"].with_context(
            no_mail_to_attendees=True,
            mail_create_nolog=True,
        ).create(vals)

        if event.google_meet_url and not event.videocall_location:
            event.videocall_location = event.google_meet_url

        if event.attendee_ids:
            event.attendee_ids.with_context(
                no_mail_to_attendees=False,
                mail_notify_force_send=True,
            )._send_invitation_emails()

        event_attachments = self.env["ir.attachment"]
        for att in self.attachment_ids:
            event_attachments |= att.copy({
                "res_model": "calendar.event",
                "res_id": event.id,
            })

        if self.candidate_id and self.candidate_id.resume_url:
            resume_att = self._fetch_resume_attachment(event)
            if resume_att:
                event_attachments |= resume_att

        if event_attachments and partner_ids:
            event.message_post(
                body=_("Interview materials attached."),
                attachment_ids=event_attachments.ids,
                partner_ids=partner_ids,
                subtype_xmlid="mail.mt_comment",
                force_send=True,
            )

        return {
            "type": "ir.actions.act_window",
            "res_model": "calendar.event",
            "view_mode": "form",
            "res_id": event.id,
            "target": "current",
        }

    def _fetch_resume_attachment(self, event):
        url = self.candidate_id.resume_url
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - network fetch, don't fail the whole flow
            _logger.warning("Resume fetch failed for %s: %s", url, exc)
            return False

        filename = urlparse(url).path.rsplit("/", 1)[-1] or "resume.pdf"
        name = self.candidate_id.partner_name or self.candidate_id.name or "candidate"
        return self.env["ir.attachment"].create({
            "name": f"{name} - resume.pdf" if filename.lower().endswith(".pdf") else filename,
            "datas": base64.b64encode(resp.content),
            "res_model": "calendar.event",
            "res_id": event.id,
            "mimetype": resp.headers.get("Content-Type", "application/pdf"),
        })
