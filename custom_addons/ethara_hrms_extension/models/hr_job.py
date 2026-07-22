import html
import logging
import secrets
from datetime import timedelta
from string import Template

from odoo import api, fields, models
from odoo.exceptions import UserError

from .res_config_settings import CONFIG_PARAM_JOB_APPROVERS, parse_user_ids

_logger = logging.getLogger(__name__)

# How long an approval link stays valid after the JD is submitted.
APPROVAL_LINK_VALIDITY_DAYS = 7


class HrJob(models.Model):
    _inherit = 'hr.job'

    slug = fields.Char(string='Slug', index=True, copy=False)
    summary = fields.Text(string='Summary')
    job_location = fields.Char(string='Job Location')

    employment_type = fields.Selection(
        [
            ('full_time', 'Full-time'),
            ('part_time', 'Part-time'),
            ('contract', 'Contract'),
            ('internship', 'Internship'),
            ('freelance', 'Freelance'),
        ],
        string='Employment Type',
    )
    work_mode = fields.Selection(
        [
            ('on_site', 'On-site'),
            ('remote', 'Remote'),
            ('hybrid', 'Hybrid'),
        ],
        string='Work Mode',
    )
    experience_level = fields.Selection(
        [
            ('fresher', 'Fresher (0-1 yr)'),
            ('junior', 'Junior (1-3 yrs)'),
            ('mid', 'Mid (3-6 yrs)'),
            ('senior', 'Senior (6-10 yrs)'),
            ('lead', 'Lead (10+ yrs)'),
        ],
        string='Experience Level',
    )
    experience_years = fields.Float(string='Experience Years')
    salary_bracket = fields.Char(string='Salary Bracket')
    budget_ctc = fields.Float(
        string='Budget CTC (LPA)', digits=(10, 2),
        help='Maximum CTC budgeted for this position. Applicants whose '
             'expected CTC exceeds it are flagged during screening.',
    )

    responsibility_ids = fields.One2many(
        'hr.job.responsibility', 'job_id', string='Responsibilities'
    )
    benefit_ids = fields.One2many(
        'hr.job.benefit', 'job_id', string='Benefits'
    )
    preferred_skill_ids = fields.Many2many(
        'hr.skill', 'hr_job_preferred_skill_rel', 'job_id', 'skill_id',
        string='Preferred Skills',
    )
    skill_keywords = fields.Text(string='Skill Keywords')

    is_featured = fields.Boolean(string='Featured')
    posted_at = fields.Datetime(string='Posted At')
    urgency_level = fields.Selection(
        [
            ('1', 'Low'),
            ('2', 'Normal'),
            ('3', 'Elevated'),
            ('4', 'High'),
            ('5', 'Critical'),
        ],
        string='Urgency Level',
    )
    screening_prompt = fields.Text(string='Screening Prompt')

    approval_status = fields.Selection(
        [
            ('draft', 'Draft'),
            ('requested', 'Requested'),
            ('posted', 'Posted'),
            ('rejected', 'Rejected'),
            ('withdrawn', 'Withdrawn'),
        ],
        string='Approval Status',
        default='draft',
        tracking=True,
    )
    approval_requested_at = fields.Datetime(string='Approval Requested At')
    approval_decided_at = fields.Datetime(string='Approval Decided At')
    approval_email_sent_at = fields.Datetime(string='Approval Email Sent At')

    requested_by_id = fields.Many2one('res.users', string='Requested By')
    external_requested_by = fields.Char(string='External Requested By')
    approved_by_id = fields.Many2one('res.users', string='Approved By')

    approver_ids = fields.One2many(
        'hr.job.approver', 'job_id', string='Approvers'
    )
    approval_recipient_email = fields.Char(string='Approval Recipient Emails')
    reviewed_by_email = fields.Char(string='Reviewed By Email')
    rejection_reason = fields.Text(string='Rejection Reason')
    rejected_by_id = fields.Many2one('res.users', string='Rejected By')

    # One-time approval link. The email buttons hit the approval endpoints with
    # ?token=<approval_token>&uid=<approver user id>. The token is cleared once a
    # decision is taken, so a link can only be used once.
    approval_token = fields.Char(string='Approval Token', index=True, copy=False)
    approval_token_expiry = fields.Datetime(string='Approval Link Expiry', copy=False)

    # Reason captured for the most recent deactivation (archive) of the JD.
    deactivation_reason = fields.Text(string='Deactivation Reason', copy=False)

    _sql_constraints = [
        ('slug_unique', 'unique(slug)', 'Job slug must be unique.'),
    ]

    # ------------------------------------------------------------------
    # Deactivation notifications
    # ------------------------------------------------------------------
    def write(self, vals):
        """Notify stakeholders when a live job position is deactivated.

        Catches every deactivation path (update API, delete-fallback archive,
        Odoo "Archive") by hooking the single moment ``active`` flips to False.
        """
        deactivating = self.browse()
        if 'active' in vals and not vals.get('active'):
            deactivating = self.filtered('active')  # only those currently active
        res = super().write(vals)
        if deactivating:
            reason = vals.get('deactivation_reason') or ''
            reason = reason.strip() if isinstance(reason, str) else ''
            # Prefer the real actor passed by the API (the record is sudo there);
            # fall back to the environment user for the Odoo UI archive.
            actor = self.env.context.get('deactivation_actor') or (
                self.env.user.name or '')
            for job in deactivating:
                try:
                    job.sudo()._send_deactivation_emails(reason, actor)
                except Exception as exc:
                    _logger.error(
                        'JD deactivation email failed for job %s: %s', job.id, exc)
        return res

    def _deactivation_recipients(self):
        """Emails notified on deactivation: the requester + configured approvers."""
        self.ensure_one()
        candidates = []
        if self.requested_by_id and self.requested_by_id.email:
            candidates.append(self.requested_by_id.email)
        ext = (self.external_requested_by or '').strip()
        if '@' in ext:
            candidates.append(ext)
        for user in self._get_job_approver_users():
            addr = (user.email or user.login or '').strip()
            if '@' in addr:
                candidates.append(addr)
        seen, out = set(), []
        for email in candidates:
            key = email.lower()
            if key not in seen:
                seen.add(key)
                out.append(email)
        return out

    def _send_deactivation_emails(self, reason='', actor=''):
        """Email a branded 'position deactivated' notice. Never raises."""
        self.ensure_one()
        recipients = self._deactivation_recipients()
        if not recipients:
            _logger.info('JD %s deactivated: no recipients to notify.', self.id)
            return 0
        email_from = self.env['ir.config_parameter'].sudo().get_param(
            'mail.catchall.email') or (
            self.company_id.email if self.company_id else None
        ) or 'no-reply@ethara.ai'
        body = self._render_deactivation_email(reason, actor)
        Mail = self.env['mail.mail'].sudo()
        sent = 0
        for recipient in recipients:
            try:
                mail = Mail.create({
                    'subject': 'Job Position Deactivated: %s' % (self.name or ''),
                    'email_from': email_from,
                    'email_to': recipient,
                    'body_html': body,
                    'auto_delete': False,
                })
                mail.send(raise_exception=False)
                sent += 1
            except Exception as exc:
                _logger.error(
                    'JD deactivation: failed to email %s: %s', recipient, exc)
        return sent

    def _render_deactivation_email(self, reason='', actor=''):
        self.ensure_one()
        reason_row = ''
        if reason:
            reason_row = (
                '<tr>'
                '<td style="padding:4px 24px 4px 0;font-weight:bold;color:#33343f;'
                'vertical-align:top;">Reason</td>'
                '<td style="padding:4px 0;color:#4b4c57;">%s</td>'
                '</tr>'
            ) % html.escape(reason)
        return Template(DEACTIVATION_EMAIL_TEMPLATE).safe_substitute(
            job_title=html.escape(self.name or ''),
            department=html.escape(self.department_id.name or '—'),
            location=html.escape(self.job_location or '—'),
            deactivated_by=html.escape(actor or '—'),
            deactivated_at=self._fmt_approval_dt(fields.Datetime.now()),
            reason_row=reason_row,
        )

    # ------------------------------------------------------------------
    # Approval workflow
    # ------------------------------------------------------------------
    @api.model
    def _get_job_approver_users(self):
        """Return the res.users configured as job-position approvers."""
        ids = parse_user_ids(
            self.env['ir.config_parameter'].sudo().get_param(
                CONFIG_PARAM_JOB_APPROVERS, '',
            )
        )
        if not ids:
            return self.env['res.users'].sudo().browse()
        return self.env['res.users'].sudo().browse(ids).exists()

    @api.model
    def _resolve_approval_token(self, token):
        """Return the job matching a live approval token (or an empty record)."""
        token = (token or '').strip()
        if not token:
            return self.sudo().browse()
        return self.sudo().search([('approval_token', '=', token)], limit=1)

    def _generate_approval_token(self):
        """Assign a fresh single-use token + expiry to this job."""
        self.ensure_one()
        token = secrets.token_urlsafe(32)
        self.sudo().write({
            'approval_token': token,
            'approval_token_expiry': fields.Datetime.now() + timedelta(
                days=APPROVAL_LINK_VALIDITY_DAYS),
        })
        return token

    def _approval_base_url(self):
        return (self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', '') or '').rstrip('/')

    def _approval_urls(self, uid, base_url=None):
        """Return the view / approve / reject URLs for a given approver uid."""
        self.ensure_one()
        base_url = base_url if base_url is not None else self._approval_base_url()
        query = 'token=%s&uid=%s' % (self.approval_token or '', uid)
        return {
            'view': '%s/api/v1/job/approval/view?%s' % (base_url, query),
            'approve': '%s/api/v1/job/approval/approve?%s' % (base_url, query),
            'reject': '%s/api/v1/job/approval/reject?%s' % (base_url, query),
        }

    @staticmethod
    def _fmt_approval_dt(value):
        if not value:
            return '—'
        return value.strftime('%d %b %Y, %I:%M %p UTC')

    def _render_approval_email(self, user, base_url=None):
        """Build the branded approval-request email HTML for one approver."""
        self.ensure_one()
        urls = self._approval_urls(user.id, base_url)
        requester = (
            self.external_requested_by
            or (self.requested_by_id.name if self.requested_by_id else '')
            or 'A recruiter'
        )
        return Template(APPROVAL_EMAIL_TEMPLATE).safe_substitute(
            approver_name=html.escape(user.name or 'there'),
            requester=html.escape(requester),
            job_title=html.escape(self.name or ''),
            department=html.escape(self.department_id.name or '—'),
            location=html.escape(self.job_location or '—'),
            requested_at=self._fmt_approval_dt(self.approval_requested_at),
            expires_at=self._fmt_approval_dt(self.approval_token_expiry),
            view_url=urls['view'],
            approve_url=urls['approve'],
            reject_url=urls['reject'],
        )

    def _send_approval_emails(self, approvers=None):
        """Email the branded approval request to every configured approver.

        Returns the number of emails actually queued. Never raises so it can be
        called safely from an API request handler.
        """
        self.ensure_one()
        if approvers is None:
            approvers = self._get_job_approver_users()
        base_url = self._approval_base_url()
        email_from = self.env['ir.config_parameter'].sudo().get_param(
            'mail.catchall.email') or (
            self.company_id.email if self.company_id else None
        ) or 'no-reply@ethara.ai'
        Mail = self.env['mail.mail'].sudo()
        sent = 0
        for user in approvers:
            recipient = (user.email or user.login or '').strip()
            if not recipient:
                _logger.warning(
                    'JD approval: approver %s (id=%s) has no email, skipped.',
                    user.name, user.id)
                continue
            try:
                mail = Mail.create({
                    'subject': 'JD Awaiting Approval: %s' % (self.name or ''),
                    'email_from': email_from,
                    'email_to': recipient,
                    'body_html': self._render_approval_email(user, base_url),
                    'auto_delete': False,
                })
                mail.send(raise_exception=False)
                sent += 1
            except Exception as exc:
                _logger.error(
                    'JD approval: failed to email approver %s: %s',
                    recipient, exc)
        if sent:
            self.sudo().write({'approval_email_sent_at': fields.Datetime.now()})
        return sent

    def _submit_for_approval(self):
        """Move the JD to 'requested', mint a token, and email the approvers.

        Returns {'approver_count', 'emails_sent'}. Safe to call from the API.
        """
        self.ensure_one()
        approvers = self._get_job_approver_users()
        self._generate_approval_token()
        now = fields.Datetime.now()
        self.sudo().write({
            'approval_status': 'requested',
            'approval_requested_at': self.approval_requested_at or now,
            'approval_decided_at': False,
            'approved_by_id': False,
            'rejected_by_id': False,
            'rejection_reason': False,
            'approval_recipient_email': ', '.join(
                (u.email or u.login) for u in approvers if (u.email or u.login)
            ) or False,
        })
        sent = self._send_approval_emails(approvers)
        return {'approver_count': len(approvers), 'emails_sent': sent}

    def _approve_jd(self, user):
        """Approve the JD: post it and record the approver."""
        self.ensure_one()
        now = fields.Datetime.now()
        self.sudo().write({
            'approval_status': 'posted',
            'approved_by_id': user.id,
            'reviewed_by_email': user.email or user.login or False,
            'approval_decided_at': now,
            'posted_at': self.posted_at or now,
            'approval_token': False,
            'approval_token_expiry': False,
        })

    def _reject_jd(self, user, reason):
        """Reject the JD: record the approver and the mandatory reason."""
        self.ensure_one()
        now = fields.Datetime.now()
        self.sudo().write({
            'approval_status': 'rejected',
            'rejected_by_id': user.id,
            'reviewed_by_email': user.email or user.login or False,
            'rejection_reason': (reason or '').strip(),
            'approval_decided_at': now,
            'approval_token': False,
            'approval_token_expiry': False,
        })

    def action_send_for_approval(self):
        """Odoo form button: submit the JD for approval."""
        for job in self:
            if not job._get_job_approver_users():
                raise UserError(
                    "No job approvers are configured. Add them in "
                    "Settings → HRMS Recruitment → Job Position Approvers."
                )
            job._submit_for_approval()
        return True


# ---------------------------------------------------------------------------
# Approval request email (branded, matches the Ethara.AI design).
# Placeholders use $name syntax (string.Template) to avoid clashing with the
# many literal { } braces in the inline CSS.
# ---------------------------------------------------------------------------
APPROVAL_EMAIL_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
</head>
<body style="margin:0;padding:0;background-color:#f2f3f7;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background-color:#f2f3f7;padding:24px 0;font-family:Arial,Helvetica,sans-serif;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0"
               style="max-width:600px;width:100%;background-color:#f7f8fb;border-radius:10px;">
          <tr>
            <td style="padding:28px 32px 16px 32px;">
              <span style="font-size:22px;font-weight:bold;color:#e6007e;">Ethara</span><span style="font-size:22px;font-weight:bold;color:#1b1b3a;">.AI</span>
            </td>
          </tr>
          <tr><td style="padding:0 32px;"><div style="border-top:1px solid #e3e5ec;"></div></td></tr>
          <tr>
            <td style="padding:24px 32px 8px 32px;color:#33343f;font-size:15px;line-height:1.6;">
              <p style="margin:0 0 16px 0;">Hi <strong>$approver_name</strong>,</p>
              <p style="margin:0 0 20px 0;"><strong>$requester</strong> created or updated the JD for <strong>$job_title</strong> and it is awaiting approval.</p>
              <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0;">
                <tr>
                  <td style="padding:4px 24px 4px 0;font-weight:bold;color:#33343f;vertical-align:top;">Department</td>
                  <td style="padding:4px 0;color:#4b4c57;">$department</td>
                </tr>
                <tr>
                  <td style="padding:4px 24px 4px 0;font-weight:bold;color:#33343f;vertical-align:top;">Location</td>
                  <td style="padding:4px 0;color:#4b4c57;">$location</td>
                </tr>
                <tr>
                  <td style="padding:4px 24px 4px 0;font-weight:bold;color:#33343f;vertical-align:top;">Requested at</td>
                  <td style="padding:4px 0;color:#4b4c57;">$requested_at</td>
                </tr>
                <tr>
                  <td style="padding:4px 24px 4px 0;font-weight:bold;color:#33343f;vertical-align:top;">Link expires</td>
                  <td style="padding:4px 0;color:#4b4c57;">$expires_at</td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 32px 4px 32px;">
              <a href="$view_url" style="display:inline-block;background-color:#2563eb;color:#ffffff;text-decoration:none;font-weight:bold;font-size:14px;padding:12px 24px;border-radius:8px;">View full JD</a>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 32px 8px 32px;">
              <a href="$approve_url" style="display:inline-block;background-color:#1da25a;color:#ffffff;text-decoration:none;font-weight:bold;font-size:14px;padding:12px 24px;border-radius:8px;margin-right:12px;">Approve JD</a>
              <a href="$reject_url" style="display:inline-block;background-color:#dc3b3b;color:#ffffff;text-decoration:none;font-weight:bold;font-size:14px;padding:12px 24px;border-radius:8px;">Reject JD</a>
            </td>
          </tr>
          <tr><td style="padding:6px 32px 0 32px;color:#6b6c78;font-size:13px;">A rejection reason is required before the JD is rejected.</td></tr>
          <tr>
            <td style="padding:14px 32px 8px 32px;color:#4b4c57;font-size:14px;line-height:1.6;">
              You can review the complete job description using the &ldquo;View full JD&rdquo; link before deciding. This JD will be posted automatically on the careers page after approval. The link can be used only once.
            </td>
          </tr>
          <tr><td style="padding:8px 32px;"><div style="border-top:1px solid #e3e5ec;"></div></td></tr>
          <tr>
            <td style="padding:8px 32px 28px 32px;color:#8a8b96;font-size:12px;line-height:1.6;">
              <div style="font-weight:bold;color:#33343f;margin-bottom:4px;">Ethara.AI</div>
              <div>5th Floor, Plot <a href="https://maps.google.com/?q=Udyog+Vihar+Phase+1+Sector+20+Gurugram" style="color:#2563eb;text-decoration:underline;">No. 273, Udyog Vihar Phase 1, Sector 20, Gurugram, Haryana 122016</a></div>
              <div style="margin-top:4px;"><a href="https://www.ethara.ai" style="color:#e6007e;text-decoration:none;">www.ethara.ai</a></div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Deactivation notice email (branded, no action buttons).
# ---------------------------------------------------------------------------
DEACTIVATION_EMAIL_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
</head>
<body style="margin:0;padding:0;background-color:#f2f3f7;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background-color:#f2f3f7;padding:24px 0;font-family:Arial,Helvetica,sans-serif;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0"
               style="max-width:600px;width:100%;background-color:#f7f8fb;border-radius:10px;">
          <tr>
            <td style="padding:28px 32px 16px 32px;">
              <span style="font-size:22px;font-weight:bold;color:#e6007e;">Ethara</span><span style="font-size:22px;font-weight:bold;color:#1b1b3a;">.AI</span>
            </td>
          </tr>
          <tr><td style="padding:0 32px;"><div style="border-top:1px solid #e3e5ec;"></div></td></tr>
          <tr>
            <td style="padding:24px 32px 8px 32px;color:#33343f;font-size:15px;line-height:1.6;">
              <p style="margin:0 0 16px 0;">Hello,</p>
              <p style="margin:0 0 20px 0;">The job position <strong>$job_title</strong> has been <strong>deactivated</strong> and is no longer visible on the careers page.</p>
              <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0;">
                <tr>
                  <td style="padding:4px 24px 4px 0;font-weight:bold;color:#33343f;vertical-align:top;">Department</td>
                  <td style="padding:4px 0;color:#4b4c57;">$department</td>
                </tr>
                <tr>
                  <td style="padding:4px 24px 4px 0;font-weight:bold;color:#33343f;vertical-align:top;">Location</td>
                  <td style="padding:4px 0;color:#4b4c57;">$location</td>
                </tr>
                <tr>
                  <td style="padding:4px 24px 4px 0;font-weight:bold;color:#33343f;vertical-align:top;">Deactivated by</td>
                  <td style="padding:4px 0;color:#4b4c57;">$deactivated_by</td>
                </tr>
                <tr>
                  <td style="padding:4px 24px 4px 0;font-weight:bold;color:#33343f;vertical-align:top;">Deactivated at</td>
                  <td style="padding:4px 0;color:#4b4c57;">$deactivated_at</td>
                </tr>
                $reason_row
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:14px 32px 8px 32px;color:#4b4c57;font-size:14px;line-height:1.6;">
              If this was not expected, please contact the recruitment team. The position can be reactivated from the recruiter dashboard.
            </td>
          </tr>
          <tr><td style="padding:8px 32px;"><div style="border-top:1px solid #e3e5ec;"></div></td></tr>
          <tr>
            <td style="padding:8px 32px 28px 32px;color:#8a8b96;font-size:12px;line-height:1.6;">
              <div style="font-weight:bold;color:#33343f;margin-bottom:4px;">Ethara.AI</div>
              <div>5th Floor, Plot <a href="https://maps.google.com/?q=Udyog+Vihar+Phase+1+Sector+20+Gurugram" style="color:#2563eb;text-decoration:underline;">No. 273, Udyog Vihar Phase 1, Sector 20, Gurugram, Haryana 122016</a></div>
              <div style="margin-top:4px;"><a href="https://www.ethara.ai" style="color:#e6007e;text-decoration:none;">www.ethara.ai</a></div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
