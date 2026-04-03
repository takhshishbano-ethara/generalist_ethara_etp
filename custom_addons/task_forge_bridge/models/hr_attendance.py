from odoo import models, fields, api


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    geo_coordinates = fields.Char(
        string='Geo Coordinates',
        help='Latitude,Longitude captured at punch-in',
    )
    geo_location = fields.Char(
        string='Location Name',
        help='Human-readable location name',
    )
    tasks_done = fields.Integer(
        string='Tasks Done',
        compute='_compute_tasks_done',
        store=False,
    )

    @api.depends('employee_id', 'check_in')
    def _compute_tasks_done(self):
        TaskLog = self.env.get('task.forge.log')
        for rec in self:
            if TaskLog and rec.employee_id and rec.check_in:
                date_str = rec.check_in.date()
                rec.tasks_done = TaskLog.sudo().search_count([
                    ('employee_id', '=', rec.employee_id.id),
                    ('date', '=', date_str),
                    ('state', '=', 'completed'),
                ])
            else:
                rec.tasks_done = 0
