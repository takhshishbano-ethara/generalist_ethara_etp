from odoo import models, fields, tools


class HrLeaveBucket(models.Model):
    """Read-only view: one row per (active employee, Ethara leave type) with the
    employee's allocated / taken / remaining days and the type's entitlement.
    Powers the per-employee balance cards (view-only)."""
    _name = 'hr.leave.bucket'
    _description = 'Employee Leave Bucket (per type)'
    _auto = False
    _rec_name = 'leave_type_id'
    _order = 'employee_id, leave_type_id'

    employee_id = fields.Many2one('hr.employee', string='Employee', readonly=True)
    leave_type_id = fields.Many2one('hr.leave.type', string='Leave Type', readonly=True)
    code = fields.Char(string='Code', readonly=True)
    color = fields.Integer(string='Color', readonly=True)
    entitlement = fields.Float(string='Entitlement', readonly=True,
                               help='Maximum days that can be allocated for this type.')
    allocated = fields.Float(string='Allocated', readonly=True)
    taken = fields.Float(string='Taken', readonly=True)
    remaining = fields.Float(string='Remaining', readonly=True)
    can_give = fields.Float(string='Can Still Give', readonly=True,
                            help='Entitlement minus what is already allocated.')

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW hr_leave_bucket AS (
                SELECT
                    row_number() OVER (ORDER BY e.id, t.sequence, t.id) AS id,
                    e.id  AS employee_id,
                    t.id  AS leave_type_id,
                    t.ethara_leave_code AS code,
                    t.color AS color,
                    COALESCE(t.default_annual_days, 0)            AS entitlement,
                    COALESCE(al.allocated, 0)                     AS allocated,
                    COALESCE(lv.taken, 0)                         AS taken,
                    COALESCE(al.allocated, 0) - COALESCE(lv.taken, 0) AS remaining,
                    GREATEST(COALESCE(t.default_annual_days, 0)
                             - COALESCE(al.allocated, 0), 0)      AS can_give
                FROM hr_employee e
                CROSS JOIN hr_leave_type t
                LEFT JOIN (
                    SELECT employee_id, holiday_status_id,
                           SUM(number_of_days) AS allocated
                    FROM hr_leave_allocation
                    WHERE state = 'validate'
                    GROUP BY employee_id, holiday_status_id
                ) al ON al.employee_id = e.id AND al.holiday_status_id = t.id
                LEFT JOIN (
                    SELECT employee_id, holiday_status_id,
                           SUM(number_of_days) AS taken
                    FROM hr_leave
                    WHERE state = 'validate'
                    GROUP BY employee_id, holiday_status_id
                ) lv ON lv.employee_id = e.id AND lv.holiday_status_id = t.id
                WHERE t.ethara_leave_code IS NOT NULL
                  AND e.active = true
            )
        """)
