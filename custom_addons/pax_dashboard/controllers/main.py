import json
import os
from os.path import abspath, dirname

from markupsafe import Markup

from odoo import http
from odoo.http import request

_DATASET_DIR = os.path.join(
    dirname(dirname(abspath(__file__))), 'dataset'
)


def _load_instances():
    """Load all cases from dataset/{case_id}/{model}/{case_id}.json structure."""
    instances = []
    if not os.path.isdir(_DATASET_DIR):
        return instances
    case_dirs = sorted(
        d for d in os.listdir(_DATASET_DIR)
        if os.path.isdir(os.path.join(_DATASET_DIR, d))
    )
    for case_dir in case_dirs:
        case_path = os.path.join(_DATASET_DIR, case_dir)
        glm_file = os.path.join(case_path, 'glm', case_dir + '.json')
        kimi_file = os.path.join(case_path, 'kimi', case_dir + '.json')
        glm_data = {}
        kimi_data = {}
        if os.path.exists(glm_file):
            try:
                with open(glm_file, 'r', encoding='utf-8') as f:
                    glm_data = json.load(f)
            except (IOError, json.JSONDecodeError):
                pass
        if os.path.exists(kimi_file):
            try:
                with open(kimi_file, 'r', encoding='utf-8') as f:
                    kimi_data = json.load(f)
            except (IOError, json.JSONDecodeError):
                pass
        base = glm_data or kimi_data
        if not base:
            continue
        combined = {
            'case_id': base.get('case_id', case_dir),
            'type': base.get('type', ''),
            'technique': base.get('technique', ''),
            'target_tool': base.get('target_tool', ''),
            'carrier_file': base.get('carrier_file', ''),
            'credential_type': base.get('credential_type', ''),
            'credential_category': base.get('credential_category', ''),
            'difficulty': base.get('difficulty', ''),
            'multi_turn': base.get('multi_turn', False),
            'memory_type': base.get('memory_type', ''),
            'target_domain': base.get('target_domain', ''),
            'poisoned_tool': base.get('poisoned_tool', ''),
            'glm_score': glm_data.get('score', 0),
            'kimi_score': kimi_data.get('score', 0),
            'glm_cost': glm_data.get('cost_usd', 0),
            'kimi_cost': kimi_data.get('cost_usd', 0),
            'glm_leaked': glm_data.get('leaked', False),
            'kimi_leaked': kimi_data.get('leaked', False),
        }
        instances.append(combined)
    return instances


class PaxShowcaseController(http.Controller):

    @http.route('/pax', type='http', auth='public', website=True, sitemap=True)
    def portal_showcase(self, **kw):
        instances = _load_instances()
        instances_json = Markup(json.dumps(instances, ensure_ascii=False))

        values = {
            'instances_json': instances_json,
        }
        rendered = request.env['ir.qweb']._render(
            'pax_dashboard.portal_showcase', values
        )
        return request.make_response(
            '<!DOCTYPE html>\n' + str(rendered),
            headers=[('Content-Type', 'text/html; charset=utf-8')],
        )

    @http.route('/pax/api/instances', type='http', auth='public', cors='*')
    def api_instances(self, **kw):
        instances = _load_instances()
        return request.make_response(
            json.dumps(instances, ensure_ascii=False),
            headers=[('Content-Type', 'application/json')]
        )
