import json
import os
from os.path import abspath, dirname

from markupsafe import Markup

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    dirname(dirname(abspath(__file__))), 'data', 'castiel_data.json'
)


def _load_data():
    try:
        with open(_DATA_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class CastielShowcaseController(http.Controller):

    @http.route('/castiel', type='http', auth='public', website=True, sitemap=True)
    def portal_showcase(self, **kw):
        data = _load_data()
        values = {
            'data_json': Markup(json.dumps(data, ensure_ascii=False)),
        }
        rendered = request.env['ir.qweb']._render(
            'castiel_dashboard.portal_showcase', values
        )
        return request.make_response(
            '<!DOCTYPE html>\n' + str(rendered),
            headers=[('Content-Type', 'text/html; charset=utf-8')],
        )

    @http.route('/castiel/api/data', type='http', auth='public', cors='*')
    def api_data(self, **kw):
        try:
            with open(_DATA_PATH, 'r', encoding='utf-8') as f:
                data = f.read()
        except FileNotFoundError:
            data = '{}'
        return request.make_response(
            data,
            headers=[('Content-Type', 'application/json')]
        )
