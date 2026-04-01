from odoo import models, fields, api, _
# from . websocket import trigger_async_ws_message

class KuberaNotification(models.Model):
    _name = 'kubera.notification'
    _description = 'Kubera Notification'
    _order = "create_date desc"

    res_model = fields.Char(string="Model Name")
    res_id = fields.Integer(string="Record ID")
    title = fields.Char(string="Title", required=True)
    message = fields.Text(string="Message", required=True)
    is_read = fields.Boolean(string="Read", default=False)
    user_id = fields.Many2one('res.users', string="User")
    date = fields.Datetime(string="Date", default=fields.Datetime.now)
    priority = fields.Selection([
        ('0', 'Low'),
        ('1', 'Normal'),
        ('2', 'High')
    ], string="Priority", default='1')
    status = fields.Selection([('0', 'Open'), ('1', 'Closed'), ('2', 'Critical')], string="Status")
    model_ref_id = fields.Many2one('ir.model', string="Model Reference")
    redirect_url = fields.Char('Redirect Url')

    @api.model
    def create(self, vals):
        res = super(KuberaNotification, self).create(vals)
        # websocket_server_url = self.env['ir.config_parameter'].sudo().get_param('websocket.server_url')
        # if websocket_server_url:
        #     for r in res:
        #         operation_type = {
        #             "operation_type": "notification",
        #             'record': []
        #         }
        #         trigger_async_ws_message(self, text=operation_type, room=f"room-{r.user_id.id}")
        return res

    def write(self, vals):
        res = super(KuberaNotification, self).write(vals)
        # if vals.get('title') or vals.get('message') or vals.get('is_read'):
        #     websocket_server_url = self.env['ir.config_parameter'].sudo().get_param('websocket.server_url')
        #     if websocket_server_url:
        #         operation_type = {
        #             "operation_type": "notification",
        #             'record': []
        #         }
        #         trigger_async_ws_message(self, text=operation_type, room=f"room-{self.user_id.id}")
        return res
