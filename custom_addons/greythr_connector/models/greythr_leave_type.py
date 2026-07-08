from odoo import fields, models


class GreythrLeaveType(models.Model):
    _name = "greythr.leave.type"
    _description = "greytHR Leave Type"
    _order = "name"

    instance_id = fields.Many2one(
        "greythr.instance",
        required=True,
        ondelete="cascade",
        index=True,
    )
    external_code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    holiday_status_id = fields.Many2one(
        "hr.leave.type",
        string="Odoo Leave Type",
        ondelete="set null",
    )
    last_sync_at = fields.Datetime(readonly=True)
    raw_payload = fields.Text(readonly=True)

    _sql_constraints = [
        (
            "uniq_instance_code",
            "UNIQUE(instance_id, external_code)",
            "greytHR leave type code must be unique per instance.",
        ),
    ]
