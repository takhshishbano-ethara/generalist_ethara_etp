import json
import os
from os.path import abspath, dirname

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    dirname(dirname(abspath(__file__))), 'data', 'kraken_instances.json'
)


class KrakenShowcaseController(http.Controller):

    @http.route('/kraken', type='http', auth='public', website=True, sitemap=True)
    def portal_showcase(self, **kw):
        icp = request.env['ir.config_parameter'].sudo()
        trajectories_url = icp.get_param(
            'kraken_dashboard.trajectories_url',
            default='https://github.com/Ethara-Ai/Kraken-Dataset'
        )
        dataset_url = icp.get_param(
            'kraken_dashboard.dataset_url',
            default='https://huggingface.co/datasets/ethara/Kraken'
        )
        return request.render('kraken_dashboard.portal_showcase', {
            'trajectories_url': trajectories_url,
            'dataset_url': dataset_url,
        })

    @http.route('/kraken/api/instances', type='http', auth='public', cors='*')
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
