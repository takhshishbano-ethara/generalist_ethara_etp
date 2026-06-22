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
        return records

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
