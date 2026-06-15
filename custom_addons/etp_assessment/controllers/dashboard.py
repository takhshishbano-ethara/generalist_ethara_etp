# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request


class EtpDashboardController(http.Controller):

    @http.route("/etp_assessment/dashboard_data", type="jsonrpc", auth="user")
    def get_dashboard_data(self, filters=None):
        return request.env["etp.assessment"].get_dashboard_data(filters=filters)
