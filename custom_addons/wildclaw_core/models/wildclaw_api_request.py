from odoo import fields, models


class WildclawApiRequest(models.Model):
    _name = "wildclaw.api.request"
    _description = "WildClaw Mock API Request Log (per-sandbox HTTP audit trail)"
    _order = "request_time desc, id desc"

    # FK is stored as int; wrappers maintain their own FK on their own sandbox model.
    # Cross-wrapper sandbox linkage is by sandbox_ref (model + id) to avoid coupling.
    sandbox_model = fields.Char(string="Sandbox Model", index=True)
    sandbox_id_int = fields.Integer(string="Sandbox ID", index=True)
    task_id = fields.Char(string="Task ID", index=True)

    service_name = fields.Char(string="Service", index=True)
    method = fields.Char(string="HTTP Method")
    path = fields.Char(string="Path")
    query_params = fields.Text(string="Query Params (JSON)")
    request_body = fields.Text(string="Request Body")
    status_code = fields.Integer(string="Status Code")
    response_body = fields.Text(string="Response Body")
    request_time = fields.Datetime(string="Request Time")
    duration_ms = fields.Float(string="Duration (ms)")
