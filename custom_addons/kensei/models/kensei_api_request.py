from odoo import models, fields, api


class KenseiApiRequest(models.Model):
    _name = "kensei.api.request"
    _description = "Kensei Mock API Request Log"
    _order = "request_time desc, id desc"

    sandbox_id = fields.Many2one(
        "kensei.sandbox", string="Sandbox", ondelete="cascade", index=True
    )
    kensei_id = fields.Many2one(
        related="sandbox_id.kensei_id", store=True, readonly=True
    )
    task_id = fields.Char(
        related="kensei_id.task_id", string="Task ID", store=True, index=True
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
