import logging
import threading

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.modules.registry import Registry

from .role_map import resolve_role_ids

_logger = logging.getLogger(__name__)


PROJECT_STATE_START = 'start'
PROJECT_STATE_PAUSE = 'pause'
PROJECT_STATE_CLOSE = 'close'
PROJECT_STATE_COMPLETE = 'complete'

PROJECT_STATE_SELECTION = [
    (PROJECT_STATE_START, 'Started'),
    (PROJECT_STATE_PAUSE, 'Paused'),
    (PROJECT_STATE_CLOSE, 'Closed'),
    (PROJECT_STATE_COMPLETE, 'Completed'),
]

VALID_PROJECT_STATES = tuple(k for k, _label in PROJECT_STATE_SELECTION)

TEAM_FIELDS = ('assigned_tpm_ids', 'assigned_pl_ql_ids', 'assigned_rnd_ids')

WELCOME_TEMPLATE_XMLID = 'ethara_project.mail_template_ethara_project_welcome'


class EtharaProject(models.Model):
    _name = 'ethara.project'
    _description = 'Ethara Project'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(
        string='Project Name',
        required=True,
        tracking=True,
    )
    client_name = fields.Char(
        string='Client Name',
        required=True,
        tracking=True,
    )
    internal_project_name = fields.Char(
        string='Internal Project Name',
        tracking=True,
    )
    project_goal = fields.Text(
        string='Project Goal',
    )
    start_date = fields.Date(
        string='Start Date',
        tracking=True,
    )
    end_date = fields.Date(
        string='End Date',
        tracking=True,
    )

    state = fields.Selection(
        selection=PROJECT_STATE_SELECTION,
        string='Status',
        default=PROJECT_STATE_START,
        required=True,
        tracking=True,
        index=True,
        copy=False,
    )

    attachment_ids = fields.One2many(
        comodel_name='ethara.project.attachment',
        inverse_name='project_id',
        string='Attachments',
        copy=True,
    )

    assigned_tpm_ids = fields.Many2many(
        comodel_name='hr.employee',
        relation='ethara_project_tpm_rel',
        column1='project_id',
        column2='employee_id',
        string='Assigned TPM',
        domain=lambda self: [('user_id.user_role', 'in', resolve_role_ids(self.env, 'tpm'))],
        help='Employees whose user_role is the TPM api.role.',
    )
    assigned_pl_ql_ids = fields.Many2many(
        comodel_name='hr.employee',
        relation='ethara_project_pl_ql_rel',
        column1='project_id',
        column2='employee_id',
        string='Assigned PL / QL',
        domain=lambda self: [('user_id.user_role', 'in', resolve_role_ids(self.env, 'pl_ql'))],
        help='Employees whose user_role is one of the PL / QC / QR api.role records.',
    )
    assigned_rnd_ids = fields.Many2many(
        comodel_name='hr.employee',
        relation='ethara_project_rnd_rel',
        column1='project_id',
        column2='employee_id',
        string='Assigned R&D',
        domain=lambda self: [('user_id.user_role', 'in', resolve_role_ids(self.env, 'rnd'))],
        help='Employees whose user_role is the R&D api.role.',
    )

    attachment_count = fields.Integer(
        string='Attachment Count',
        compute='_compute_attachment_count',
    )

    cto_user_ids = fields.Many2many(
        comodel_name='res.users',
        string='CTO Users',
        compute='_compute_role_users',
        help='All active users whose api.role resolves to the CTO role. '
             'Auto-added as followers of this project.',
    )
    cfo_user_ids = fields.Many2many(
        comodel_name='res.users',
        string='CFO Users',
        compute='_compute_role_users',
        help='All active users whose api.role resolves to the CFO role. '
             'Auto-added as followers of this project.',
    )

    @api.depends()
    def _compute_role_users(self):
        Users = self.env['res.users'].sudo()
        cto_role_ids = resolve_role_ids(self.env, 'cto')
        cfo_role_ids = resolve_role_ids(self.env, 'cfo')
        cto_users = (
            Users.search([('user_role', 'in', cto_role_ids)])
            if cto_role_ids else Users.browse()
        )
        cfo_users = (
            Users.search([('user_role', 'in', cfo_role_ids)])
            if cfo_role_ids else Users.browse()
        )
        for rec in self:
            rec.cto_user_ids = cto_users
            rec.cfo_user_ids = cfo_users

    @api.depends('attachment_ids')
    def _compute_attachment_count(self):
        for rec in self:
            rec.attachment_count = len(rec.attachment_ids)

    @api.constrains('start_date', 'end_date')
    def _check_dates(self):
        for rec in self:
            if rec.start_date and rec.end_date and rec.start_date > rec.end_date:
                raise ValidationError(_('Start date must be on or before end date.'))

    def _collect_team_employee_ids(self):
        ids = set()
        for rec in self:
            for f in TEAM_FIELDS:
                ids |= set(rec[f].ids)
        return ids

    def _recompute_team_work_status(self, extra_ids=None):
        emp_ids = self._collect_team_employee_ids()
        if extra_ids:
            emp_ids |= set(extra_ids)
        if not emp_ids:
            return
        self.env['hr.employee'].sudo().browse(list(emp_ids))._recompute_ethara_work_status()

    def _ethara_team_partner_ids(self):
        self.ensure_one()
        partners = self.env['res.partner']
        for fname in TEAM_FIELDS:
            for emp in self[fname]:
                if emp.user_id and emp.user_id.partner_id:
                    partners |= emp.user_id.partner_id
                elif 'work_contact_id' in emp._fields and emp.work_contact_id:
                    partners |= emp.work_contact_id
        return partners.ids

    def _ethara_thread_partner_ids(self):
        self.ensure_one()
        partner_ids = set(self._ethara_team_partner_ids())
        partner_ids.update(self.cto_user_ids.mapped('partner_id').ids)
        partner_ids.update(self.cfo_user_ids.mapped('partner_id').ids)
        return list(partner_ids)

    def _ethara_subscribe_thread_followers(self):
        for rec in self:
            partner_ids = rec._ethara_thread_partner_ids()
            if partner_ids:
                rec.sudo().message_subscribe(partner_ids=partner_ids)

    def _ethara_post_thread_message(
        self, template_xmlid, record, partner_ids, email_values=None,
    ):
        self.ensure_one()
        if not partner_ids:
            return False
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if not template:
            _logger.warning(
                "Mail template %s not found; skipping ethara project notification.",
                template_xmlid,
            )
            return False
        try:
            rendered_body = template._render_field(
                'body_html', [record.id],
            )[record.id]
            rendered_subject = template._render_field(
                'subject', [record.id],
            )[record.id]
        except Exception:
            _logger.exception(
                "Failed to render template %s for %s(%s).",
                template_xmlid, record._name, record.id,
            )
            return False
        try:
            rendered_email_from = template._render_field(
                'email_from', [record.id],
            )[record.id]
        except Exception:
            rendered_email_from = (
                self.env.user.email_formatted
                or (self.env.company and self.env.company.email)
                or False
            )

        email_values = dict(email_values or {})
        final_subject = email_values.get('subject') or rendered_subject
        attachment_cmds = email_values.get('attachment_ids') or []
        attachment_ids = []
        for cmd in attachment_cmds:
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 3:
                attachment_ids.extend(cmd[2] or [])
            elif isinstance(cmd, int):
                attachment_ids.append(cmd)

        post_kwargs = {
            'body': rendered_body,
            'subject': final_subject,
            'partner_ids': partner_ids,
            'subtype_xmlid': 'mail.mt_comment',
            'message_type': 'comment',
        }
        if rendered_email_from:
            post_kwargs['email_from'] = rendered_email_from
        if attachment_ids:
            post_kwargs['attachment_ids'] = attachment_ids

        try:
            self.message_post(**post_kwargs)
        except Exception:
            _logger.exception(
                "Failed to post ethara project notification on %s(%s) "
                "(template %s).", self._name, self.id, template_xmlid,
            )
            return False

        if record._name != self._name or record.id != self.id:
            try:
                rec_label = record.display_name or "%s#%s" % (record._name, record.id)
                audit_body = (
                    "<p>Notification cross-posted to "
                    "<strong>%s</strong>.</p>"
                    "<p>Subject: <em>%s</em></p>"
                    "<p>Source record: %s</p>"
                ) % (self.display_name, final_subject, rec_label)
                record.message_post(
                    body=audit_body,
                    subtype_xmlid='mail.mt_note',
                    message_type='notification',
                )
            except Exception:
                _logger.exception(
                    "Failed to log audit note on %s(%s).",
                    record._name, record.id,
                )
        return True

    def _ethara_send_welcome_email(self):
        self.ensure_one()
        if self.env.context.get('ethara_skip_welcome_mail'):
            return False
        partner_ids = self._ethara_thread_partner_ids()
        if not partner_ids:
            return False
        return self._ethara_post_thread_message(
            WELCOME_TEMPLATE_XMLID,
            self,
            partner_ids,
        )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        record_ids = records.ids
        if record_ids:
            dbname = self.env.cr.dbname
            uid = self.env.uid
            ctx = dict(self.env.context)

            def _launch():
                def _run():
                    try:
                        registry = Registry(dbname)
                        with registry.cursor() as new_cr:
                            new_env = api.Environment(new_cr, uid, ctx)
                            projects = (
                                new_env['ethara.project']
                                .browse(record_ids).exists()
                            )
                            projects._recompute_team_work_status()
                            projects._ethara_subscribe_thread_followers()
                            for rec in projects:
                                try:
                                    rec._ethara_send_welcome_email()
                                except Exception:
                                    _logger.exception(
                                        "Welcome email failed for "
                                        "ethara.project %s", rec.id,
                                    )
                            new_cr.commit()
                    except Exception:
                        _logger.exception(
                            "Deferred welcome job failed for "
                            "ethara.project %s", record_ids,
                        )

                threading.Thread(target=_run, daemon=True).start()

            self.env.cr.postcommit.add(_launch)
        return records

    def write(self, vals):
        touches_status = 'state' in vals or any(f in vals for f in TEAM_FIELDS)
        old_ids = self._collect_team_employee_ids() if touches_status else set()
        result = super().write(vals)
        if touches_status:
            self._recompute_team_work_status(extra_ids=old_ids)
        if any(f in vals for f in TEAM_FIELDS):
            self._ethara_subscribe_thread_followers()
        return result

    def action_set_state(self, new_state):
        if new_state not in VALID_PROJECT_STATES:
            raise ValidationError(_(
                "Invalid project state '%s'. Allowed: %s"
            ) % (new_state, ', '.join(VALID_PROJECT_STATES)))
        self.write({'state': new_state})
        return True

    def action_start(self):
        return self.action_set_state(PROJECT_STATE_START)

    def action_pause(self):
        return self.action_set_state(PROJECT_STATE_PAUSE)

    def action_close(self):
        return self.action_set_state(PROJECT_STATE_CLOSE)

    def action_complete(self):
        return self.action_set_state(PROJECT_STATE_COMPLETE)
