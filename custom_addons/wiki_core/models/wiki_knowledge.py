import html
import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class WikiFaq(models.Model):
    _name = 'wiki.faq'
    _description = 'Wiki FAQ'
    _order = 'group_sequence, sequence, id'

    name = fields.Char(string='Question', required=True)
    answer = fields.Text(string='Answer', required=True)
    group = fields.Char(string='Group', required=True, default='General')
    group_sequence = fields.Integer(string='Group Order', default=10)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)


class WikiCategory(models.Model):
    _name = 'wiki.category'
    _description = 'Wiki Category'
    _order = 'sequence, id'

    name = fields.Char(string='Title', required=True)
    description = fields.Char(string='Description')
    icon = fields.Char(string='Icon Key', help='UI icon identifier.')
    route_key = fields.Char(string='Route Key',
                            help='Frontend route the card opens.')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)


class WikiUpdate(models.Model):
    """A published policy / Recent Update shown on the Foundation page.

    Publishing one notifies every user and employee: an in-app
    kubera.notification record plus an email. Notification fires
    automatically on create and can be re-triggered manually from the form
    (the "Send Notification" button) if the automatic send was skipped or
    failed.
    """
    _name = 'wiki.update'
    _description = 'Wiki Recent Update'
    _order = 'date desc, id desc'

    name = fields.Char(string='Title', required=True)
    owner = fields.Char(string='Owner', help='Team or person, e.g. "HR".')
    date = fields.Date(string='Date', required=True)
    active = fields.Boolean(default=True)
    notification_sent = fields.Boolean(
        string='Notification Sent', default=False, copy=False,
        help='Set once a policy notification has been sent for this update.')

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Skip while loading module/demo data; only notify at runtime.
        if self.env.registry.ready:
            for record in records:
                try:
                    record._send_policy_notification()
                except Exception:
                    _logger.exception(
                        'Auto policy notification failed for wiki.update %s',
                        record.id)
        return records

    def action_send_notification(self):
        """Manual fallback button: (re)send the policy notification."""
        for record in self:
            record._send_policy_notification()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Notification sent',
                'message': 'Policy notification sent to all users and '
                           'employees.',
                'type': 'success',
                'sticky': False,
            },
        }

    def _send_policy_notification(self):
        """Notify every internal user (in-app) and email all users and
        employees about this policy.

        Records and mails are created in batch and the mails are *queued*
        (not sent inline) — Odoo's outgoing-mail cron delivers them. This
        keeps publishing a policy fast and non-blocking even with hundreds
        of recipients.
        """
        self.ensure_one()
        title = 'New Policy: %s' % (self.name or '')
        body = 'A new policy "%s" has been published%s. ' \
               'Please review it in the Company Wiki.' % (
                   self.name or '',
                   (' by %s' % self.owner) if self.owner else '')

        users = self.env['res.users'].sudo().search(
            [('active', '=', True), ('share', '=', False)])

        # In-app notifications: one batch insert, not one call per user.
        notif_vals = [{
            'title': title,
            'message': body,
            'user_id': user.id,
            'res_model': 'wiki.update',
            'res_id': self.id,
            'priority': '2',
        } for user in users]
        if notif_vals:
            self.env['kubera.notification'].sudo().create(notif_vals)

        # Email every distinct user/employee address. Created in 'outgoing'
        # state and left for the mail-queue cron to deliver — never sent
        # inline, so the request returns immediately.
        emails = set(users.mapped('email'))
        emails |= set(self.env['hr.employee'].sudo().search([]).mapped('work_email'))
        body_html = '<p>%s</p>' % html.escape(body)
        mail_vals = [{
            'subject': title,
            'body_html': body_html,
            'email_to': address,
            'auto_delete': True,
        } for address in sorted(e for e in emails if e)]
        if mail_vals:
            self.env['mail.mail'].sudo().create(mail_vals)

        self.notification_sent = True
        return True


class WikiTrainingGroup(models.Model):
    _name = 'wiki.training.group'
    _description = 'Wiki Training Group'
    _order = 'sequence, id'

    name = fields.Char(string='Group', required=True)
    sequence = fields.Integer(default=10)
    doc_ids = fields.One2many('wiki.training.doc', 'group_id', string='Documents')
    active = fields.Boolean(default=True)


class WikiTrainingDoc(models.Model):
    _name = 'wiki.training.doc'
    _description = 'Wiki Training Document'
    _order = 'sequence, id'

    name = fields.Char(string='Title', required=True)
    group_id = fields.Many2one('wiki.training.group', string='Group',
                               required=True, ondelete='cascade')
    meta = fields.Char(string='Meta', help='e.g. "Engineering · 12 pages".')
    description = fields.Text(
        string='Description',
        help='Short summary shown in the training detail pane.')
    body = fields.Text(string='Body')
    required = fields.Boolean(
        string='Required', default=False,
        help='Mark as a required document every employee must complete.')
    pages = fields.Integer(string='Pages')
    document = fields.Binary(string='Document', attachment=True)
    document_filename = fields.Char(string='Document Filename')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)


class WikiTrainingProgress(models.Model):
    """Per-employee status and progress for a training document. A missing
    record means the employee has not started the document."""
    _name = 'wiki.training.progress'
    _description = 'Wiki Training Progress'
    _order = 'write_date desc, id desc'

    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True, ondelete='cascade')
    doc_id = fields.Many2one(
        'wiki.training.doc', string='Document', required=True,
        ondelete='cascade')
    state = fields.Selection(
        [('not_started', 'Not started'), ('in_progress', 'In progress'),
         ('completed', 'Completed')],
        string='Status', required=True, default='not_started')
    progress = fields.Integer(string='Progress %', default=0)

    _employee_doc_uniq = models.Constraint(
        'unique(employee_id, doc_id)',
        'There is already a progress record for this employee and document.')
