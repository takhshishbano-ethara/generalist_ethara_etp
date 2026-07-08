from odoo import fields, models


class GreythrLeaveTransaction(models.Model):
    _name = "greythr.leave.transaction"
    _description = "greytHR Leave Transaction"
    _order = "from_date desc, id desc"

    external_transaction_id = fields.Char(required=True, index=True)
    greythr_employee_id = fields.Many2one(
        "greythr.employee",
        ondelete="set null",
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
    )
    transaction_type = fields.Selection(
        [
            ("credit", "Credit"),
            ("debit", "Debit"),
            ("opening", "Opening"),
            ("closing", "Closing"),
            ("encash", "Encashment"),
            ("lapse", "Lapse"),
            ("other", "Other"),
        ],
    )
    from_date = fields.Date()
    to_date = fields.Date()
    number_of_days = fields.Float(digits=(6, 2))
    sessions = fields.Char()
    status = fields.Selection(
        [
            ("applied", "Applied"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("cancelled", "Cancelled"),
            ("other", "Other"),
        ],
    )
    remarks = fields.Text()
    reason = fields.Text()

    last_sync_at = fields.Datetime(readonly=True)
    raw_payload = fields.Text(readonly=True)

    _sql_constraints = [
        (
            "uniq_external_txn",
            "UNIQUE(external_transaction_id)",
            "greytHR transaction id must be unique.",
        ),
    ]
