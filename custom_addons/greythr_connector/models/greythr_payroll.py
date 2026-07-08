from odoo import fields, models


class GreythrPayroll(models.Model):
    _name = "greythr.payroll"
    _description = "greytHR Payroll Record"
    _order = "period_year desc, period_month desc, id desc"
    _rec_name = "external_payslip_id"

    instance_id = fields.Many2one(
        "greythr.instance",
        required=True,
        ondelete="cascade",
        index=True,
    )
    greythr_employee_id = fields.Many2one(
        "greythr.employee",
        required=True,
        ondelete="cascade",
        index=True,
    )
    employee_id = fields.Many2one(
        "hr.employee",
        related="greythr_employee_id.employee_id",
        store=True,
        readonly=True,
        index=True,
    )
    external_payslip_id = fields.Char(index=True)
    period_year = fields.Integer(required=True, index=True)
    period_month = fields.Integer(required=True, index=True)
    payslip_date = fields.Date()
    currency = fields.Char()
    gross_salary = fields.Float(digits=(14, 2))
    net_salary = fields.Float(digits=(14, 2))
    total_earnings = fields.Float(digits=(14, 2))
    total_deductions = fields.Float(digits=(14, 2))
    tax = fields.Float(digits=(14, 2))
    paid_days = fields.Float(digits=(6, 2))
    lop_days = fields.Float(digits=(6, 2))
    status = fields.Char()
    last_sync_at = fields.Datetime(readonly=True)
    raw_payload = fields.Text(readonly=True)

    _sql_constraints = [
        (
            "uniq_employee_period",
            "unique(greythr_employee_id, period_year, period_month)",
            "Payroll for this employee/period already exists.",
        ),
    ]
