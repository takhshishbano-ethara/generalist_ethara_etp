from odoo import fields, models


class GreythrLeaveBalance(models.Model):
    _name = "greythr.leave.balance"
    _description = "greytHR Leave Balance"
    _order = "year desc, greythr_employee_id, leave_type_id"

    greythr_employee_id = fields.Many2one(
        "greythr.employee",
        required=True,
        ondelete="cascade",
        index=True,
    )
    instance_id = fields.Many2one(
        related="greythr_employee_id.instance_id",
        store=True,
        index=True,
    )
    employee_id = fields.Many2one(
        related="greythr_employee_id.employee_id",
        string="Odoo Employee",
        store=True,
        index=True,
    )
    leave_type_id = fields.Many2one(
        "greythr.leave.type",
        ondelete="set null",
        index=True,
    )
    holiday_status_id = fields.Many2one(
        related="leave_type_id.holiday_status_id",
        string="Odoo Leave Type",
        store=False,
    )
    year = fields.Integer(required=True, index=True)

    opening_balance = fields.Float(digits=(12, 2))
    granted = fields.Float(digits=(12, 2))
    availed = fields.Float(digits=(12, 2))
    applied = fields.Float(digits=(12, 2))
    lapsed = fields.Float(digits=(12, 2))
    deducted = fields.Float(digits=(12, 2))
    encashed = fields.Float(digits=(12, 2))
    current_balance = fields.Float(digits=(12, 2))

    last_sync_at = fields.Datetime(readonly=True)
    raw_payload = fields.Text(readonly=True)

    _sql_constraints = [
        (
            "uniq_employee_type_year",
            "UNIQUE(greythr_employee_id, leave_type_id, year)",
            "Only one balance row per employee, leave type and year.",
        ),
    ]

    def name_get(self):
        return [
            (
                rec.id,
                "%s / %s / %s"
                % (
                    rec.greythr_employee_id.name or "",
                    rec.leave_type_id.name or "",
                    rec.year,
                ),
            )
            for rec in self
        ]
