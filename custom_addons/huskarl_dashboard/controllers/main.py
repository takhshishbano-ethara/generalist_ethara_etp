import json
import os
from os.path import abspath, dirname

from markupsafe import Markup

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    dirname(dirname(abspath(__file__))), 'data', 'huskarl_instances.json'
)


class HuskarlShowcaseController(http.Controller):

    @http.route('/mars', type='http', auth='public', website=True, sitemap=True)
    def portal_showcase(self, **kw):
        try:
            with open(_DATA_PATH, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raw_data = []

        instances_json = Markup(json.dumps(raw_data, ensure_ascii=False))

        values = {
            'instances_json': instances_json,
        }
        rendered = request.env['ir.qweb']._render(
            'huskarl_dashboard.portal_showcase', values
        )
        return request.make_response(
            '<!DOCTYPE html>\n' + str(rendered),
            headers=[('Content-Type', 'text/html; charset=utf-8')],
        )

    @http.route('/mars/api/instances', type='http', auth='public', cors='*')
    def api_instances(self, **kw):
        try:
            with open(_DATA_PATH, 'r', encoding='utf-8') as f:
                data = f.read()
        except FileNotFoundError:
            data = '[]'
        return request.make_response(
            data,
            headers=[('Content-Type', 'application/json')]
        )
