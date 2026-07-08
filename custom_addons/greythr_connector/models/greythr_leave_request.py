from odoo import fields, models


class GreythrLeaveRequest(models.Model):
    _name = "greythr.leave.request"
    _description = "greytHR Leave Request"
    _order = "from_date desc, id desc"
    _rec_name = "external_request_id"

    instance_id = fields.Many2one(
        "greythr.instance",
        required=True,
        ondelete="cascade",
        index=True,
    )
    external_request_id = fields.Char(index=True, copy=False)
    hr_leave_id = fields.Many2one("hr.leave", ondelete="set null", index=True)
    greythr_employee_id = fields.Many2one(
        "greythr.employee", ondelete="set null", index=True
    )
    employee_id = fields.Many2one(
        "hr.employee",
        string="Odoo Employee",
        related="greythr_employee_id.employee_id",
        store=True,
        readonly=True,
        index=True,
    )
    leave_type_id = fields.Many2one(
        "greythr.leave.type", ondelete="set null", index=True
    )
    holiday_status_id = fields.Many2one(
        "hr.leave.type",
        related="leave_type_id.holiday_status_id",
        store=False,
        readonly=True,
    )
    from_date = fields.Date()
    to_date = fields.Date()
    number_of_days = fields.Float(digits=(6, 2))
    sessions = fields.Char()
    reason = fields.Text()
    remarks = fields.Text()

    direction = fields.Selection(
        [
            ("outbound", "Odoo -> greytHR"),
            ("inbound", "greytHR -> Odoo"),
        ],
        default="outbound",
        required=True,
    )
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending", "Pending Approval"),
            ("approved", "Approved"),
            ("refused", "Refused"),
            ("cancelled", "Cancelled"),
            ("other", "Other"),
        ],
        default="draft",
    )
    push_state = fields.Selection(
        [
            ("not_sent", "Not Sent"),
            ("sent", "Sent"),
            ("failed", "Failed"),
        ],
        default="not_sent",
    )
    push_error = fields.Text(readonly=True, copy=False)
    last_sync_at = fields.Datetime(readonly=True)
    raw_payload = fields.Text(readonly=True)

    _sql_constraints = [
        (
            "uniq_external_request",
            "unique(instance_id, external_request_id)",
            "External request already recorded for this instance.",
        ),
    ]

    def action_push_now(self):
        for rec in self:
            if rec.direction != "outbound" or not rec.hr_leave_id:
                continue
            action = "create" if not rec.external_request_id else "approve"
            rec.instance_id.sudo()._push_leave_to_greythr(
                rec.hr_leave_id, action=action
            )
        return True
