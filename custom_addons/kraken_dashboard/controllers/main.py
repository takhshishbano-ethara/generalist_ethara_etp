import json
import os
from os.path import abspath, dirname

from markupsafe import Markup

from odoo import http
from odoo.http import request

_DATA_PATH = os.path.join(
    dirname(dirname(abspath(__file__))), 'data', 'kraken_instances.json'
)

MODEL_MAP = {
    'glm5': 'GLM-5',
    'nova': 'Nova-2-Lite',
}


def _flatten_instances(raw_data):
    rows = []
    for inst in raw_data:
        repo_url = inst.get('repo_url', '')
        repo = repo_url.replace('https://github.com/', '') if repo_url else ''
        base = {
            'instance_id': inst['instance_id'],
            'repo_url': repo_url,
            'repo': repo,
            'pr_url': inst.get('pr_url', ''),
            'issue_url': inst.get('issue_url', ''),
            'language': inst.get('language', 'Python'),
            'difficulty': inst.get('difficulty', ''),
            'gold_speedup': inst.get('gold_speedup', 1.0),
            'f2p_count': inst.get('f2p_count', 0),
            'p2p_count': inst.get('p2p_count', 0),
        }
        for key, model_name in MODEL_MAP.items():
            m = inst.get(key, {})
            row = dict(base)
            row['model'] = model_name
            row['hsr'] = m.get('hsr', 0)
            row['speedup_lm'] = m.get('speedup_lm', 0)
            row['speedup_adjusted'] = m.get('speedup_adjusted', 0)
            row['tests_passed'] = m.get('tests_passed', 0)
            row['tests_total'] = m.get('tests_total', 0)
            row['correctness_pct'] = m.get('correctness_pct', 0)
            row['files_modified'] = m.get('files_modified', 0)
            row['tool_calls'] = m.get('tool_calls', 0)
            row['cost'] = m.get('cost', 0)
            row['time_secs'] = m.get('time_secs', 0)
            row['outcome'] = m.get('outcome', 'fail')
            row['pass_at_1'] = m.get('pass_at_1', 'Fail')
            rows.append(row)
    return rows


class KrakenShowcaseController(http.Controller):

    @http.route('/kraken', type='http', auth='public', website=True, sitemap=True)
    def portal_showcase(self, **kw):
        try:
            with open(_DATA_PATH, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raw_data = []

        flat_rows = _flatten_instances(raw_data)
        instances_json = Markup(json.dumps(flat_rows, ensure_ascii=False))

        values = {
            'instances_json': instances_json,
        }
        rendered = request.env['ir.qweb']._render(
            'kraken_dashboard.portal_showcase', values
        )
        return request.make_response(
            '<!DOCTYPE html>\n' + str(rendered),
            headers=[('Content-Type', 'text/html; charset=utf-8')],
        )

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
