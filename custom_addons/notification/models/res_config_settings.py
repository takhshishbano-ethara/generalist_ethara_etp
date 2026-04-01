from odoo import models, fields, api
import requests
import json

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    websocket_server_url = fields.Char('Websocket Server URL', config_parameter='websocket.server_url', default='ws://13.200.75.36:9005')

