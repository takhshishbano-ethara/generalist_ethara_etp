from odoo import models, fields, api
import requests
import json
import logging

_logger = logging.getLogger(__name__)

ROLE_FIELD_MAP = {
    'project_lead': 'lead',
    'project_qc_reviewer': 'qc_reviewer',
    'project_tasker': 'tasker',
    'project_aire': 'aire',
    'project_swe': 'swe',
}


class Project(models.Model):
    _inherit = 'project.project'

    project_seq = fields.Char(string='Sequence', default='New')
    project_attachments = fields.Many2many('ir.attachment', 'project_project_hr_employee_attachment_rel', string='Attachments')
    internal_project_name = fields.Char(string='Internal Project')
    client_name = fields.Char(string='Client Name')
    internal_client_name = fields.Char(string='Internal Client Name')
    project_category = fields.Selection([('technical', 'Technical'), ('stem', 'Stem'), ('non_stem', 'Non Stem')], string='Project Category')
    project_type = fields.Selection([('single_turn', 'Single Turn'), ('multi_turn', 'Multi Turn')], default='single_turn')
    sample_task_number = fields.Integer(string='Sample Task Number')
    project_lead = fields.Many2many('hr.employee','project_project_hr_employee_lead_rel', string='Lead')
    project_aire = fields.Many2many('hr.employee','project_project_hr_employee_aire_rel', string='AI Research Engineer (AIRE)')
    project_swe = fields.Many2many('hr.employee','project_project_hr_employee_swe_rel', string='Software Engineer (SWE)')
    ai_generated_description = fields.Text(string='AI Generated Description')
    # Whatsapp Group
    whatsapp_group_name = fields.Char(string='Whatsapp Group Name')
    whatsapp_group_members = fields.Many2many('whatsapp.group.members', string='Whatsapp Group Members')
    # Slack Group
    slack_channel_name = fields.Char(string='Slack Channel Name')
    slack_members = fields.Many2many('hr.employee', 'project_project_hr_employee_slack_member_rel', string='Slack Members')
    slack_channel_id = fields.Many2one('discuss.channel', string="Slack Channel")
    slack_channels_id = fields.Char(string="Slack Channel")
    # Google Drive
    google_drive_id = fields.Many2one('google.drive.file', string='Google Drive ID')
    # kick off email
    kick_off_to_mails = fields.Char(string='Kick Off To Mails')
    kick_off_subject = fields.Char(string='Kick Off Subject')
    kick_off_body = fields.Text('Kick Off Body')

    is_pl_stage_completed = fields.Boolean(string='Is PL Stage Completed')
    is_aire_stage_completed = fields.Boolean(string='Is AIRE Stage Completed')
    is_swe_stage_completed = fields.Boolean(string='Is SWE Stage Completed')
    y_project_type = fields.Selection([('non-stem', 'Non Stem'), ('stem', 'Stem'), ('technical', 'Technical')], default='non-stem')
    # PL Portal
    project_guide_lines = fields.Many2many('google.drive.file','project_project_google_drive_file_guide_lines_rel', string='Project Guide Lines')
    skill_tags = fields.Many2many('project.skills', 'project_project_project_skills_rel', string='Project Skill Tags')
    project_qc_reviewer = fields.Many2many('hr.employee', 'project_project_hr_employee_qc_reviewer_rel',
                                           string='QC Reviewers')
    project_tasker = fields.Many2many('hr.employee', 'project_project_hr_employee_tasker_rel', string='Tasker')
    meeting_date = fields.Datetime(string='Meeting Date')
    meeting_link = fields.Char(string='Meeting Link')
    meeting_agenda = fields.Text(string='Meeting Agenda')
    meeting_to_mails = fields.Char(string='To Mails')
    meeting_cc_mails = fields.Char(string='CC Mails')
    meeting_bcc_mails = fields.Char(string='BCC Mails')
    meeting_subject = fields.Char(string='Meeting Subject')
    meeting_body = fields.Text(string='Meeting Body')
    meeting_attachments = fields.Many2many('ir.attachment', 'project_project_ir_attachment_meeting_rel', string='Attachments')

    # Extra
    project_experiment_design = fields.Many2many('google.drive.file','project_project_google_drive_experiment_design_rel', string='Project Experiment Design')
    project_research_document = fields.Many2many('google.drive.file','project_project_google_drive_research_document_rel', string='Project Research Document')
    project_infrastructure_requirement = fields.Many2many('google.drive.file','project_project_google_drive_infrastructure_requirement_rel', string='Project Infrastructure Requirement')
    training_event_id = fields.Many2one('calendar.event', 'Training Meeting linked', ondelete='cascade')
    ai_recommendation_tags = fields.Many2many('project.ai.recommendation', 'project_project_ai_recommendation_rel', string='Project AI Recommendation Tags')
    research_notes = fields.Char(string='Research Notes')
    rating_configuration = fields.Selection([('stars', 'Stars'), ('binary', 'Binary'), ('rubric', 'Rubric')])
    task_template_type = fields.Many2one('task.template.type', string="Task Template Type")
    lock_ttl= fields.Integer(string="Lock TTL (Minute)")
    daily_quota_per_tasker = fields.Integer(string="Daily Quota per Tasker")
    approval_target = fields.Integer(string="Approval Target")
    non_stemp_project_status = fields.Selection([('draft', 'Draft'), ('not_started', 'Not Started'), ('closed', 'Closed'), ('paused', 'Paused'), ('production', 'Production'), ('cancel', 'Cancel')], default='not_started')
    base_project_task = fields.One2many('base.project.task', 'project_id', string='Base Project Task')
    member_history_ids = fields.One2many('project.member.history', 'project_id', string='Member History')

    is_rubrics_required = fields.Boolean(string='Rubrics Required', default=False)
    is_justification_required = fields.Boolean(string='Justification Required', default=False)
    rubric_category_ids = fields.One2many('rubric.category', 'project_id', string='Rubric Categories')

    is_response_required = fields.Boolean(string='Response Fields Required', default=False)
    no_of_responses = fields.Integer(string='Number of Responses', default=0)
    response_config_ids = fields.One2many('project.response.config', 'project_id', string='Response Configurations')

    is_timer_enabled = fields.Boolean(string='Timer Enabled', default=False)

    _TASKER_ACTIVE_TASK_STATES = ('in_progress', 'ack', 'escalated')

    @api.model
    def _validate_tasker_assignment(self, tasker_ids, exclude_project_id=None):
        """Returns ``(errors, removals)``. Caller MUST apply ``removals`` via
        :meth:`_apply_tasker_removals` only when ``errors`` is empty.

        A project is 'running' iff ``non_stemp_project_status == 'production'``.
        A tasker is blocked when already on another running project with a
        ``task.forge.log`` in an active state; otherwise queued for removal
        from every other project. Pass ``exclude_project_id`` on update to
        skip the target project itself; pass ``None`` on create.
        """
        Project = self.env['project.project'].sudo()
        Employee = self.env['hr.employee'].sudo()
        Log = self.env['task.forge.log'].sudo() if 'task.forge.log' in self.env else None

        errors = []
        removals = {}
        seen = set()

        for tid in tasker_ids or []:
            if not tid or tid in seen:
                continue
            seen.add(tid)

            tasker = Employee.browse(tid)
            if not tasker.exists():
                continue

            domain = [('project_tasker', 'in', [tid])]
            if exclude_project_id:
                domain.append(('id', '!=', exclude_project_id))
            other_projects = Project.search(domain)
            if not other_projects:
                continue

            running_others = other_projects.filtered(
                lambda p: p.non_stemp_project_status == 'production'
            )

            is_blocked = False
            if running_others and Log is not None:
                active_log = Log.search([
                    ('employee_id', '=', tid),
                    ('project_id', 'in', running_others.ids),
                    ('state', 'in', list(self._TASKER_ACTIVE_TASK_STATES)),
                ], limit=1)
                if active_log:
                    errors.append({
                        'tasker_id': tasker.id,
                        'tasker_name': tasker.name,
                        'task_id': active_log.id,
                        'task_name': active_log.name,
                        'task_sequence': active_log.sequence,
                        'project_id': active_log.project_id.id,
                        'project_name': active_log.project_id.name,
                        'message': (
                            "Tasker '%s' is currently working on task '%s' "
                            "(ID %s) in running project '%s' (ID %s). "
                            "Cannot assign to another project until the tasker is idle."
                        ) % (
                            tasker.name,
                            active_log.sequence or active_log.name,
                            active_log.id,
                            active_log.project_id.name,
                            active_log.project_id.id,
                        ),
                    })
                    is_blocked = True

            if is_blocked:
                continue

            for proj in other_projects:
                removals.setdefault(proj.id, []).append(tid)

        return errors, removals

    @api.model
    def _apply_tasker_removals(self, removals):
        """Apply removal dict produced by :meth:`_validate_tasker_assignment`."""
        if not removals:
            return
        Project = self.env['project.project'].sudo()
        for project_id, tasker_ids in removals.items():
            if not tasker_ids:
                continue
            Project.browse(project_id).write({
                'project_tasker': [(3, tid) for tid in tasker_ids],
            })

    def create_slack_channel(self):
        if not self.slack_channel_name:
            _logger.info('No slack_channel_name set for project %s, skipping', self.name)
            return

        users_with_slack = []
        users_without_slack = []
        for emp in self.slack_members:
            if not emp.user_id:
                continue
            user = emp.user_id.sudo()
            if not user.slack_user_ref:
                try:
                    user.lookup_slack_id_by_email()
                except Exception as e:
                    _logger.error('Slack lookup failed for %s: %s', user.login, str(e))
            if user.slack_user_ref:
                users_with_slack.append(user)
            else:
                users_without_slack.append(user)

        if not users_with_slack:
            _logger.warning('No slack members with Slack accounts for project %s. Cannot create channel.', self.name)
            for user in users_without_slack:
                result = user.invite_to_slack()
                if result.get('success'):
                    _logger.info('Slack invitation sent to %s', user.login)
                else:
                    _logger.warning('Slack invite for %s: %s', user.login, result.get('error', ''))
            return

        admin_id = users_with_slack[0].id
        all_user_ids = [u.id for u in users_with_slack]

        try:
            channel_name = '%s-%s' % (self.slack_channel_name, self.id)
            DiscussChannel = self.env['discuss.channel'].sudo()
            result = DiscussChannel.create_slack_channel(
                channel_name=channel_name.lower().replace(' ', '-')[:80],
                admin_id=admin_id,
                user_ids=all_user_ids,
            )

            if result.get('success'):
                self.slack_channels_id = result.get('slack_channel_id', '')
                _logger.info('Slack channel created for project %s: %s', self.name, result.get('slack_channel_id'))
            else:
                _logger.error('Slack channel creation failed for %s: %s', self.name, result.get('error', ''))

        except Exception as err:
            _logger.error('create_slack_channel error for project %s: %s', self.name, str(err))

        for user in users_without_slack:
            result = user.invite_to_slack()
            if result.get('success'):
                _logger.info('Slack invitation sent to %s', user.login)
            else:
                _logger.warning('Slack invite for %s: %s', user.login, result.get('error', ''))

    def kick_off_send_mail(self):
        outgoing_server_name = self.env['ir.mail_server'].sudo().search([], limit=1).name or "atech@yopmail.com"
        for record in self:
            template = self.env.ref('project_extension.email_notification_template_view')
            email_values = {
                'email_from': outgoing_server_name,
                'email_to': record.kick_off_to_mails,
                'subject': record.kick_off_subject,
                'body_html': record.kick_off_body
            }
            template.sudo().send_mail(record.id, email_values=email_values, force_send=True)
        return True

    def create_google_drive_folders(self):
        try:
            self.ensure_one()
            parent_wizard = self.env['google.drive.wizard'].create({
                'name': self.name,
                'upload_type': 'folder'
            })
            parent_folder = parent_wizard._create_folder()

            if parent_folder:
                self.google_drive_id = parent_folder.id
                for sub in ["Internal", "External"]:
                    drive_record = self.env['google.drive.wizard'].create({
                        'name': sub,
                        'upload_type': 'folder',
                        'parent_folder_id': parent_folder.id
                    })._create_folder()
                    if self.project_attachments:
                        self.upload_attachments_in_drive(drive_id = drive_record)
            if self.project_attachments:
                for att in self.project_attachments:
                    att.sudo().unlink()
        except Exception as err:
            print(f"An error occurred: {err}")

    def upload_attachments_in_drive(self, drive_id=None):
        import base64
        for attach in self.project_attachments:
            drive_record = self.env['google.drive.wizard'].create({
                'name': attach.name,
                'upload_type': 'file',
                'file_content': attach.datas,
                'parent_folder_id': drive_id.id if drive_id else None,
            })._upload_file()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        from odoo.addons.task_forge_core.services.background_tasks import run_many_in_background

        for vals in vals_list:
            vals['project_seq'] = self.env['ir.sequence'].next_by_code('project.project')
        projects = super(Project, self).create(vals_list)
        for project, vals in zip(projects, vals_list):
            project._sync_member_history(vals)

            db_name = self.env.cr.dbname
            uid = self.env.uid
            project_id = project.id

            def _run_background(pid=project_id):
                run_many_in_background(db_name, uid, [
                    {'func': 'kick_off_send_mail', 'model': 'project.project', 'record_id': pid},
                    {'func': 'create_google_drive_folders', 'model': 'project.project', 'record_id': pid},
                    {'func': 'create_slack_channel', 'model': 'project.project', 'record_id': pid},
                ])

            self.env.cr.postcommit.add(_run_background)
        return projects

    def write(self, vals):

        old_members = {}
        for field_name in ROLE_FIELD_MAP:
            if field_name in vals:
                old_members[field_name] = {proj.id: set(proj[field_name].ids) for proj in self}

        result = super(Project, self).write(vals)

        for field_name, role in ROLE_FIELD_MAP.items():
            if field_name not in vals:
                continue
            for proj in self:
                old_ids = old_members.get(field_name, {}).get(proj.id, set())
                new_ids = set(proj[field_name].ids)

                added = new_ids - old_ids
                removed = old_ids - new_ids

                History = self.env['project.member.history'].sudo()

                for emp_id in added:
                    existing = History.search([
                        ('project_id', '=', proj.id),
                        ('employee_id', '=', emp_id),
                        ('role', '=', role),
                        ('state', '=', 'active'),
                    ], limit=1)
                    if not existing:
                        History.create({
                            'project_id': proj.id,
                            'employee_id': emp_id,
                            'role': role,
                            'start_date': fields.Date.today(),
                        })

                for emp_id in removed:
                    active_record = History.search([
                        ('project_id', '=', proj.id),
                        ('employee_id', '=', emp_id),
                        ('role', '=', role),
                        ('state', '=', 'active'),
                    ], limit=1)
                    if active_record:
                        active_record.action_offboard(
                            reason='Removed from project team',
                        )

        return result

    def _sync_member_history(self, vals):
        History = self.env['project.member.history'].sudo()
        for field_name, role in ROLE_FIELD_MAP.items():
            if field_name not in vals:
                continue
            m2m_commands = vals[field_name]
            emp_ids = set()
            if isinstance(m2m_commands, list):
                for cmd in m2m_commands:
                    if isinstance(cmd, (list, tuple)):
                        if cmd[0] == 6:
                            emp_ids = set(cmd[2])
                        elif cmd[0] == 4:
                            emp_ids.add(cmd[1])
            for emp_id in emp_ids:
                History.create({
                    'project_id': self.id,
                    'employee_id': emp_id,
                    'role': role,
                    'start_date': fields.Date.today(),
                })

class TaskTemplateType(models.Model):
    _name = 'task.template.type'
    _description = 'Task Template Type'

    name = fields.Char(string='Name')
    project_key = fields.Char(string='Project Key')
    model_name = fields.Char(string='Model')
    mapping_field_name = fields.Char(string='Mapping Field')

class ProjectAttachment(models.Model):
    _name = 'project.attachment'
    _description = 'Project Attachment'

    project_id = fields.Many2one('project.project', string='Project')
    image_url = fields.Char(string='Image URL')

class ProjectSkills(models.Model):
    _name = 'project.skills'
    _description = 'Project Skills'

    name = fields.Char(string='Name')

class ProjectAIRecommendation(models.Model):
    _name = 'project.ai.recommendation'
    _description = 'Project AI Recommendation'

    name = fields.Char(string='Name')

class ProjectCategory(models.Model):
    _name = 'project.category'
    _description = 'Project Category'

    name = fields.Char(string='Name')


class ProjectProjectStage(models.Model):
    _inherit = 'project.project.stage'

    lable_name = fields.Char(string='Lable Name')

class WhatsappGroupMembers(models.Model):
    _name = 'whatsapp.group.members'
    _description = 'Whatsapp Group Members'

    name = fields.Char(string='Name')
    email = fields.Char(string='Email')
    country_code = fields.Char(string='Country Code')
    phone_number = fields.Char(string='Phone Number')
