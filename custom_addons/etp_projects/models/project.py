import logging
import random

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


TEAM_FIELDS = ('project_tasker', 'project_qc_reviewer', 'project_lead', 'project_tpm')

# api_prefix MUST end with `?project_id=` so the Flutter app can append the id;
# sequences leave gaps for Overview/Tasks/Budget/Analytics tabs added by extensions.
DEFAULT_API_MAPPINGS = (
    {
        'sequence': 20,
        'table_name': 'Team',
        'field_name': 'team',
        'api_prefix': '/v2/project_team_member_list?project_id=',
    },
    {
        'sequence': 60,
        'table_name': 'Logs',
        'field_name': 'logs',
        'api_prefix': '/v1/get_notification_grouped?project_id=',
    },
    {
        'sequence': 40,
        'table_name': 'Budget',
        'field_name': 'budget',
        'api_prefix': '/v1/etp_projects/budget/info?project_id=',
    },
    {
        'sequence': 40,
        'table_name': 'Tasks',
        'field_name': 'tasks',
        'api_prefix': '/v1/etp_projects/budget/estimation/list?project_id=',
    },
    {
        'sequence': 40,
        'table_name': 'Batch',
        'field_name': 'batch',
        'api_prefix': '/v1/etp_projects/budget/estimation/batch_view?project_id=',
    },
)


class ProjectProject(models.Model):
    _inherit = 'project.project'

    project_classification = fields.Selection(
        selection=[
            ('internal', 'Internal'),
            ('external', 'External'),
        ],
        string='Project Classification',
        default='internal',
        required=True,
        index=True,
        help="Internal projects are managed from the Project app and their "
             "tasks appear in the Tasks menus. External projects are managed "
             "from the dedicated External Projects app.",
    )
    api_map_ids = fields.One2many(
        comodel_name='etp.external.project.api.map',
        inverse_name='project_id',
        string='API Mappings',
        help="API table/field mappings connected to this external project.",
    )
    connected_table = fields.Char(string='Connected Table')
    category_url = fields.Char(string='Category URL')
    tasker_url = fields.Char(string='Tasker URL')
    budget_refresh_url = fields.Char(string='Budget Refresh URL')

    project_tpm = fields.Many2many(
        'hr.employee',
        'project_project_hr_employee_tpm_rel',
        string='TPM',
        help='Technical Program Managers assigned to this project.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        skip_defaults = self.env.context.get('skip_default_api_maps')
        ApiMap = self.env['etp.external.project.api.map']
        for record, vals in zip(records, vals_list):
            if not skip_defaults and not vals.get('api_map_ids'):
                ApiMap.sudo().create([
                    {
                        **mapping,
                        'project_id': record.id,
                        'api_prefix': f"{mapping['api_prefix']}{record.id}",
                    }
                    for mapping in DEFAULT_API_MAPPINGS
                ])
            changed = {f for f in TEAM_FIELDS if f in vals}
            if changed:
                record._cascade_team_assignments(changed)
        for record in records:
            try:
                record._etp_send_welcome_email()
            except Exception:
                _logger.exception(
                    "Welcome email failed for project %s", record.id,
                )
        return records

    _ETP_WELCOME_ROLE_FIELDS = (
        'project_lead',
        'project_tpm',
        'project_qc_reviewer',
        'project_tasker',
        'project_aire',
        'project_swe',
    )

    def _etp_employee_partner(self, employee):
        if not employee:
            return self.env['res.partner']
        if employee.user_id and employee.user_id.partner_id:
            return employee.user_id.partner_id
        if 'work_contact_id' in employee._fields and employee.work_contact_id:
            return employee.work_contact_id
        return self.env['res.partner']

    def _etp_budget_recipient_partner_ids(self):
        self.ensure_one()
        partners = self.env['res.partner']
        if self.create_uid and self.create_uid.partner_id:
            partners |= self.create_uid.partner_id
        if self.user_id and self.user_id.partner_id:
            partners |= self.user_id.partner_id
        if 'assigned_team_ids' in self._fields and self.assigned_team_ids:
            partners |= self.assigned_team_ids.mapped('partner_id')
        for fname in self._ETP_WELCOME_ROLE_FIELDS:
            if fname not in self._fields:
                continue
            for emp in self[fname]:
                partners |= self._etp_employee_partner(emp)
        return partners.ids

    def _etp_pick_budget_thread_anchor(self, record):
        if record._name == 'etp.project.aws.budget':
            return record
        if 'project_budget_id' in record._fields and record.project_budget_id:
            return record.project_budget_id
        return self

    def _etp_post_budget_message(
        self, template_xmlid, record, partner_ids, email_values=None,
    ):
        self.ensure_one()
        if not partner_ids:
            return False
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if not template:
            _logger.warning(
                "Mail template %s not found; skipping budget notification.",
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

        thread_anchor = self._etp_pick_budget_thread_anchor(record)
        try:
            thread_anchor.message_post(**post_kwargs)
        except Exception:
            _logger.exception(
                "Failed to post budget notification on %s(%s) "
                "(template %s).", thread_anchor._name, thread_anchor.id,
                template_xmlid,
            )
            return False

        if record._name != thread_anchor._name or record.id != thread_anchor.id:
            try:
                rec_label = record.display_name or f"{record._name}#{record.id}"
                audit_body = (
                    "<p>Budget notification cross-posted to "
                    f"<strong>{thread_anchor.display_name}</strong>.</p>"
                    f"<p>Subject: <em>{final_subject}</em></p>"
                    f"<p>Source record: {rec_label}</p>"
                )
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

    def _etp_send_welcome_email(self):
        self.ensure_one()
        if self.env.context.get('etp_skip_welcome_mail'):
            return False
        partner_ids = self._etp_budget_recipient_partner_ids()
        if not partner_ids:
            return False
        return self._etp_post_budget_message(
            'etp_projects.mail_template_project_welcome',
            self,
            partner_ids,
        )

    def write(self, vals):
        result = super().write(vals)
        changed = {f for f in TEAM_FIELDS if f in vals}
        if changed:
            self._cascade_team_assignments(changed)
        return result

    def _cascade_team_assignments(self, changed_fields):
        for project in self:
            if 'project_tasker' in changed_fields or 'project_qc_reviewer' in changed_fields:
                project._cascade_role(
                    subjects=project.project_tasker,
                    candidates=project.project_qc_reviewer,
                    target_field='task_forge_qr_id',
                )
            if 'project_qc_reviewer' in changed_fields or 'project_lead' in changed_fields:
                project._cascade_role(
                    subjects=project.project_qc_reviewer,
                    candidates=project.project_lead,
                    target_field='task_forge_pl_id',
                )
            if 'project_lead' in changed_fields or 'project_tpm' in changed_fields:
                project._cascade_role(
                    subjects=project.project_lead,
                    candidates=project.project_tpm,
                    target_field='task_forge_tpm_id',
                )

    def _cascade_role(self, subjects, candidates, target_field):
        if not subjects or not candidates:
            return
        Employee = self.env['hr.employee']
        if target_field not in Employee._fields:
            _logger.warning(
                "Field %s missing on hr.employee; skipping cascade", target_field,
            )
            return
        candidate_ids = candidates.ids
        for subject in subjects:
            current = subject[target_field]
            if current and current.id in candidate_ids:
                continue
            chosen_id = random.choice(candidate_ids)
            subject.sudo().write({target_field: chosen_id})
