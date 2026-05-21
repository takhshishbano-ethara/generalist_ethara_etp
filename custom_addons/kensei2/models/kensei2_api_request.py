from odoo import models, fields, api


class Kensei2ApiRequest(models.Model):
    _name = "kensei2.api.request"
    _description = "Kensei2 Mock API Request Log"
    _order = "request_time desc, id desc"

    sandbox_id = fields.Many2one(
        "kensei2.sandbox", string="Sandbox", ondelete="cascade", index=True
    )
    kensei2_id = fields.Many2one(
        related="sandbox_id.kensei2_id", store=True, readonly=True
    )
    task_id = fields.Char(
        related="kensei2_id.task_id", string="Task ID", store=True, index=True
    )

    service_name = fields.Char(string="Service", index=True)
    method = fields.Char(string="HTTP Method")
    path = fields.Char(string="Path")
    query_params = fields.Text(string="Query Params (JSON)")
    request_body = fields.Text(string="Request Body")
    status_code = fields.Integer(string="Status Code")
    response_body = fields.Text(string="Response Body")
    request_time = fields.Datetime(string="Request Time")
    duration_ms = fields.Float(string="Duration (ms)")
