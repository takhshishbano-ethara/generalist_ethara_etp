import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    greythr_employee_ids = fields.One2many(
        "greythr.employee", "employee_id", string="greytHR Records"
    )
    greythr_leave_balance_ids = fields.One2many(
        "greythr.leave.balance", "employee_id", string="greytHR Leave Balances"
    )
    greythr_leave_transaction_ids = fields.One2many(
        "greythr.leave.transaction", "employee_id", string="greytHR Leave Transactions"
    )
    greythr_leave_request_ids = fields.One2many(
        "greythr.leave.request", "employee_id", string="greytHR Leave Requests"
    )
    greythr_payroll_ids = fields.One2many(
        "greythr.payroll", "employee_id", string="greytHR Payroll"
    )
    greythr_leave_balance_count = fields.Integer(
        compute="_compute_greythr_counts", string="greytHR Balance Count"
    )
    greythr_leave_transaction_count = fields.Integer(
        compute="_compute_greythr_counts", string="greytHR Transaction Count"
    )
    greythr_leave_request_count = fields.Integer(
        compute="_compute_greythr_counts", string="greytHR Request Count"
    )
    greythr_payroll_count = fields.Integer(
        compute="_compute_greythr_counts", string="greytHR Payroll Count"
    )

    def _compute_greythr_counts(self):
        Bal = self.env["greythr.leave.balance"]
        Tx = self.env["greythr.leave.transaction"]
        Req = self.env["greythr.leave.request"]
        Pay = self.env["greythr.payroll"]
        for rec in self:
            rec.greythr_leave_balance_count = Bal.search_count(
                [("employee_id", "=", rec.id)]
            )
            rec.greythr_leave_transaction_count = Tx.search_count(
                [("employee_id", "=", rec.id)]
            )
            rec.greythr_leave_request_count = Req.search_count(
                [("employee_id", "=", rec.id)]
            )
            rec.greythr_payroll_count = Pay.search_count(
                [("employee_id", "=", rec.id)]
            )

    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        Instance = self.env["greythr.instance"].sudo()
        instances = Instance.search(
            [("active", "=", True), ("push_employee_on_create", "=", True)]
        )
        if not instances:
            return employees
        for emp in employees:
            for instance in instances:
                try:
                    instance._push_employee_to_greythr(emp)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "greytHR employee push failed for %s (%s): %s",
                        emp.display_name,
                        emp.id,
                        exc,
                    )
        return employees
