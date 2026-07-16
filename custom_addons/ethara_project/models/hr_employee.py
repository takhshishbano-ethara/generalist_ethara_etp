from odoo import fields, models


WORK_STATUS_ALLOCATED = 'allocated'
WORK_STATUS_UNALLOCATED = 'unallocated'

WORK_STATUS_SELECTION = [
    (WORK_STATUS_UNALLOCATED, 'Unallocated'),
    (WORK_STATUS_ALLOCATED, 'Allocated'),
]


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    work_status = fields.Selection(
        selection=WORK_STATUS_SELECTION,
        string='Work Status',
        default=WORK_STATUS_UNALLOCATED,
        tracking=True,
        index=True,
        help='Allocated when the employee is part of at least one Ethara '
             'Project that is currently in the "start" state. Otherwise '
             'unallocated.',
    )

    def _recompute_ethara_work_status(self):
        if not self:
            return
        Project = self.env['ethara.project'].sudo()
        for emp in self:
            active_count = Project.search_count([
                ('state', '=', 'start'),
                '|', '|',
                ('assigned_tpm_ids', 'in', emp.id),
                ('assigned_pl_ql_ids', 'in', emp.id),
                ('assigned_rnd_ids', 'in', emp.id),
            ])
            new_status = (
                WORK_STATUS_ALLOCATED if active_count > 0
                else WORK_STATUS_UNALLOCATED
            )
            if emp.work_status != new_status:
                emp.sudo().write({'work_status': new_status})
