import base64
import csv
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .employee_import_line import (
    ROLE_API_XMLID,
    ROLE_LABELS,
    ROLE_REQUIRES_MANAGER,
)


_logger = logging.getLogger(__name__)


COLUMN_ALIASES = {
    'name': ('name', 'employee_name', 'full_name'),
    'email': ('email', 'work_email', 'email_address'),
    'employee_code': ('employee_code', 'emp_id', 'employee_id', 'code'),
    'role': ('role', 'employee_role'),
    'reporting_manager': (
        'reporting_manager', 'reporting_manager_email',
        'manager_email', 'manager', 'reports_to',
    ),
}

ROLE_TEXT_ALIASES = {
    'quality_lead': 'ql',
    'qualitylead': 'ql',
    'ql': 'ql',
    'quality_reviewer': 'qr',
    'qualityreviewer': 'qr',
    'qr': 'qr',
    'project_lead': 'pl',
    'projectlead': 'pl',
    'pl': 'pl',
    'tpm': 'tpm',
    'cto': 'cto',
    'hr': 'hr_admin',
    'hradmin': 'hr_admin',
    'hr_admin': 'hr_admin',
    'tasker': 'tasker',
}

ROLE_GROUP_XMLIDS = {
    'tasker': ['etp_user_roles.group_tasker'],
    'qr': ['etp_user_roles.group_quality_reviewer'],
    'ql': ['etp_user_roles.group_quality_lead'],
    'pl': ['etp_user_roles.group_project_lead'],
    'tpm': ['etp_user_roles.group_tpm'],
    'cto': ['etp_user_roles.group_cto'],
    'hr_admin': ['etp_user_roles.group_hr_admin', 'hr.group_hr_manager'],
}


class EmployeeImportWizard(models.TransientModel):
    _name = 'employee.import.wizard'
    _description = 'Employee CSV Import Wizard'

    csv_file = fields.Binary(string='CSV File', required=True)
    csv_filename = fields.Char(string='Filename')
    create_user = fields.Boolean(string='Create Login User', default=True)
    default_password = fields.Char(string='Default Password', default='Ethara@123')

    line_ids = fields.One2many(
        'employee.import.line', 'wizard_id', string='Preview',
    )
    state = fields.Selection(
        [('draft', 'Draft'), ('done', 'Done')], default='draft',
    )

    total_count = fields.Integer(compute='_compute_counts')
    ready_count = fields.Integer(compute='_compute_counts')
    issue_count = fields.Integer(compute='_compute_counts')

    import_count = fields.Integer(string='Employees Created', readonly=True)
    update_count = fields.Integer(string='Employees Updated', readonly=True)
    error_count = fields.Integer(string='Errors', readonly=True)
    user_count = fields.Integer(
        string='Users Created/Linked',
        compute='_compute_user_count',
        store=True,
    )
    log_text = fields.Text(string='Import Log', readonly=True)

    imported_employee_ids = fields.Many2many(
        'hr.employee', 'employee_import_wizard_employee_rel',
        'wizard_id', 'employee_id', string='Imported Employees',
    )
    imported_user_ids = fields.Many2many(
        'res.users', 'employee_import_wizard_user_rel',
        'wizard_id', 'user_id', string='Imported Users',
    )

    @api.depends('line_ids.has_issues')
    def _compute_counts(self):
        for w in self:
            w.total_count = len(w.line_ids)
            w.issue_count = sum(1 for l in w.line_ids if l.has_issues)
            w.ready_count = w.total_count - w.issue_count

    @api.depends('imported_user_ids')
    def _compute_user_count(self):
        for w in self:
            w.user_count = len(w.imported_user_ids)

    @api.model_create_multi
    def create(self, vals_list):
        wizards = super().create(vals_list)
        if not self.env.context.get('employee_import_no_auto'):
            for wizard in wizards:
                wizard._maybe_auto_import()
        return wizards

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get('employee_import_no_auto'):
            for wizard in self:
                wizard._maybe_auto_import()
        return res

    def _maybe_auto_import(self):
        self.ensure_one()
        if self.state != 'draft' or not self.line_ids:
            return
        if self.import_count or self.update_count or self.error_count or self.log_text:
            return
        self.with_context(employee_import_no_auto=True).action_import()

    @api.onchange('csv_file')
    def _onchange_csv_file(self):
        self.line_ids = [(5, 0, 0)]
        if not self.csv_file:
            return

        try:
            raw = base64.b64decode(self.csv_file).decode('utf-8-sig')
        except Exception as exc:
            raise UserError(_("Could not decode CSV file: %s") % exc)

        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            raise UserError(_("CSV file appears to be empty."))

        normalized = {
            (h or '').strip().lower().replace(' ', '_'): h
            for h in reader.fieldnames
        }
        header_map = {}
        for canonical, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in normalized:
                    header_map[canonical] = normalized[alias]
                    break

        missing = [c for c in ('name', 'email') if c not in header_map]
        if missing:
            raise UserError(_(
                "Missing required CSV columns: %s. "
                "Required columns: name, email. "
                "Optional: employee_code, role, reporting_manager."
            ) % ', '.join(missing))

        Employee = self.env['hr.employee']
        lines_cmds = []
        seq = 10

        for row in reader:
            def cell(key):
                col = header_map.get(key)
                return (row.get(col) or '').strip() if col else ''

            name = cell('name')
            email = cell('email').lower()
            employee_code = cell('employee_code').upper()
            role = self._resolve_role(cell('role'))
            manager_raw = cell('reporting_manager')

            if not any([name, email, employee_code, role, manager_raw]):
                continue

            vals = {
                'sequence': seq,
                'name': name,
                'email': email,
                'employee_code': employee_code,
                'role': role,
                'reporting_manager_email_raw': manager_raw,
            }
            if manager_raw:
                mgr = Employee.search([('work_email', '=', manager_raw)], limit=1)
                if mgr:
                    vals['reporting_manager_id'] = mgr.id
            lines_cmds.append((0, 0, vals))
            seq += 10

        if not lines_cmds:
            raise UserError(_("No data rows found in CSV."))
        self.line_ids = lines_cmds

    @staticmethod
    def _resolve_role(value):
        if not value:
            return False
        v = value.strip().lower().replace(' ', '_').replace('-', '_')
        if v in ROLE_TEXT_ALIASES:
            return ROLE_TEXT_ALIASES[v]
        for key, label in ROLE_LABELS:
            if v == label.lower().replace(' ', '_'):
                return key
        return False

    def action_import(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("No rows to import."))
        bad = sum(1 for l in self.line_ids if l.has_issues)
        if bad:
            raise UserError(_(
                "Fix all issues before importing. %d row(s) have issues."
            ) % bad)

        Employee = self.env['hr.employee'].sudo()
        Users = self.env['res.users'].sudo().with_context(active_test=False)
        base_group_id = self.env.ref('base.group_user').id

        created_emp_ids = []
        updated_emp_ids = []
        created_user_ids = []
        linked_user_ids = []
        errors = []
        log_lines = [
            f"Config: create_user={self.create_user} "
            f"default_password={'set' if self.default_password else 'EMPTY'} "
            f"lines={len(self.line_ids)}"
        ]

        for line in self.line_ids:
            user = False
            user_action = 'skipped'

            if not line.email:
                user_action = 'skipped (no email)'
                log_lines.append(f"  user {line.name}: skipped - no email")
            else:
                try:
                    existing = Users.search(
                        [('login', '=ilike', line.email)], limit=1,
                    )
                    if existing:
                        if not existing.active:
                            existing.write({'active': True})
                            log_lines.append(
                                f"  user {line.email}: reactivated archived"
                            )
                        user = existing
                        linked_user_ids.append(user.id)
                        user_action = f'linked existing id={user.id}'
                        log_lines.append(
                            f"  user {line.email}: found existing id={user.id}"
                        )
                    elif not self.create_user:
                        user_action = 'skipped (create_user off)'
                        log_lines.append(
                            f"  user {line.email}: skipped - create_user off"
                        )
                    else:
                        role_groups = self._resolve_role_group_ids(line.role)
                        group_cmds = [(4, base_group_id)] + [
                            (4, gid) for gid in role_groups
                        ]
                        user_vals = {
                            'name': line.name,
                            'login': line.email,
                            'email': line.email,
                            'group_ids': group_cmds,
                        }
                        if self.default_password:
                            user_vals['password'] = self.default_password
                        log_lines.append(
                            f"  user {line.email}: creating "
                            f"(role_groups={len(role_groups)})"
                        )
                        try:
                            user = Users.with_context(
                                no_reset_password=True,
                            ).create(user_vals)
                            created_user_ids.append(user.id)
                            user_action = f'created id={user.id}'
                            log_lines.append(
                                f"  user {line.email}: created id={user.id}"
                            )
                        except Exception as create_exc:
                            log_lines.append(
                                f"ERROR (user create): {line.email} - "
                                f"{type(create_exc).__name__}: {create_exc}"
                            )
                            _logger.exception(
                                "User create failed for line %s", line.id,
                            )
                            user = False
                            user_action = 'failed'
                except Exception as exc:
                    log_lines.append(
                        f"ERROR (user step): {line.email} - "
                        f"{type(exc).__name__}: {exc}"
                    )
                    _logger.exception("User step failed for line %s", line.id)
                    user = False
                    user_action = 'failed'

            if user and line.role:
                if user.id in linked_user_ids:
                    try:
                        role_group_ids = self._resolve_role_group_ids(line.role)
                        if role_group_ids:
                            user.write({
                                'group_ids': [(4, gid) for gid in role_group_ids],
                            })
                    except Exception as exc:
                        log_lines.append(
                            f"WARN (groups): {line.email} - "
                            f"{type(exc).__name__}: {exc}"
                        )
                        _logger.exception(
                            "Group step failed for line %s", line.id,
                        )
                try:
                    xmlid = ROLE_API_XMLID.get(line.role)
                    if xmlid:
                        api_role = self.env.ref(xmlid, raise_if_not_found=False)
                        if api_role:
                            user.write({'user_role': api_role.id})
                except Exception as exc:
                    log_lines.append(
                        f"WARN (api role): {line.email} - "
                        f"{type(exc).__name__}: {exc}"
                    )
                    _logger.exception("Role step failed for line %s", line.id)

            emp = False
            action = None
            try:
                emp = self._find_existing_employee(line)
                emp_vals = {
                    'name': line.name,
                    'work_email': line.email,
                }
                if line.employee_code:
                    emp_vals['employee_code'] = line.employee_code
                if line.reporting_manager_id:
                    emp_vals['parent_id'] = line.reporting_manager_id.id
                if user:
                    emp_vals['user_id'] = user.id

                if emp:
                    emp.write(emp_vals)
                    updated_emp_ids.append(emp.id)
                    action = 'Updated'
                else:
                    emp = Employee.create(emp_vals)
                    created_emp_ids.append(emp.id)
                    action = 'Created'
            except Exception as exc:
                errors.append(line.email or line.name)
                log_lines.append(
                    f"ERROR (employee): {line.email or line.name} - "
                    f"{type(exc).__name__}: {exc}"
                )
                _logger.exception("Employee step failed for line %s", line.id)
                continue

            try:
                hvals = self._propagate_hierarchy(emp, line)
                if hvals:
                    log_lines.append(
                        f"  hierarchy {line.email or line.name}: "
                        f"ql={hvals.get('task_forge_ql_id') or '-'} "
                        f"pl={hvals.get('task_forge_pl_id') or '-'} "
                        f"tpm={hvals.get('task_forge_tpm_id') or '-'}"
                    )
            except Exception as exc:
                log_lines.append(
                    f"WARN (hierarchy): {line.email or line.name} - {exc}"
                )
                _logger.exception("Hierarchy step failed for line %s", line.id)

            log_lines.append(
                f"{action}: {line.name} <{line.email}> role={line.role} "
                f"manager={line.reporting_manager_id.name or '-'} user={user_action}"
            )

        log_lines.append(
            f"Summary: employees_created={len(created_emp_ids)} "
            f"employees_updated={len(updated_emp_ids)} "
            f"users_created={len(created_user_ids)} "
            f"users_linked={len(linked_user_ids)} "
            f"errors={len(errors)}"
        )

        self.write({
            'state': 'done',
            'import_count': len(created_emp_ids),
            'update_count': len(updated_emp_ids),
            'error_count': len(errors),
            'log_text': '\n'.join(log_lines),
            'imported_employee_ids': [(6, 0, created_emp_ids + updated_emp_ids)],
            'imported_user_ids': [(6, 0, created_user_ids + linked_user_ids)],
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _resolve_role_group_ids(self, role_key):
        if not role_key:
            return []
        xmlids = ROLE_GROUP_XMLIDS.get(role_key) or []
        group_ids = []
        for xmlid in xmlids:
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if group:
                group_ids.append(group.id)
        return group_ids

    def _find_existing_employee(self, line):
        Employee = self.env['hr.employee']
        if line.employee_code:
            emp = Employee.search(
                [('employee_code', '=', line.employee_code)], limit=1,
            )
            if emp:
                return emp
        if line.email:
            emp = Employee.search(
                [('work_email', '=', line.email)], limit=1,
            )
            if emp:
                return emp
        return Employee.browse()

    def _propagate_hierarchy(self, emp, line):
        if not line.role or not line.reporting_manager_id:
            return {}
        manager = line.reporting_manager_id
        has_ql = 'task_forge_ql_id' in emp._fields
        has_pl = 'task_forge_pl_id' in emp._fields
        has_tpm = 'task_forge_tpm_id' in emp._fields
        vals = {}

        if line.role == 'tasker':
            if has_ql:
                vals['task_forge_ql_id'] = manager.id
            if has_pl and manager._fields.get('task_forge_pl_id') and manager.task_forge_pl_id:
                vals['task_forge_pl_id'] = manager.task_forge_pl_id.id
            if has_tpm and manager._fields.get('task_forge_tpm_id') and manager.task_forge_tpm_id:
                vals['task_forge_tpm_id'] = manager.task_forge_tpm_id.id
        elif line.role in ('ql', 'qr'):
            if has_pl:
                vals['task_forge_pl_id'] = manager.id
            if has_tpm and manager._fields.get('task_forge_tpm_id') and manager.task_forge_tpm_id:
                vals['task_forge_tpm_id'] = manager.task_forge_tpm_id.id
        elif line.role == 'pl':
            if has_tpm:
                vals['task_forge_tpm_id'] = manager.id

        if vals:
            emp.write(vals)
        return vals

    def action_reset(self):
        self.ensure_one()
        self.write({
            'state': 'draft',
            'line_ids': [(5, 0, 0)],
            'csv_file': False,
            'csv_filename': False,
            'import_count': 0,
            'update_count': 0,
            'error_count': 0,
            'log_text': False,
            'imported_employee_ids': [(5, 0, 0)],
            'imported_user_ids': [(5, 0, 0)],
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_view_imported_employees(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Imported Employees'),
            'res_model': 'hr.employee',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.imported_employee_ids.ids)],
        }

    def action_view_imported_users(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Imported Users'),
            'res_model': 'res.users',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.imported_user_ids.ids)],
        }
