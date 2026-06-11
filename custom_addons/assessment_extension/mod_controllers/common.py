import json

from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import return_Response


DEFAULT_LIMIT = 20
MAX_LIMIT = 200

ASSESSMENT_STATES = ('draft', 'in_progress', 'done', 'cancelled')
QUESTION_TYPES = (
    'image_comparison', 'text', 'coding', 'image_text', 'video',
    'eval_compare', 'prompt_writing', 'bbox_labeling',
)

PEN_QUESTION_TYPES = ('eval_compare', 'prompt_writing', 'bbox_labeling')

TYPE_PILL = {
    'eval_compare':   {'label': 'Eval Compare',   'token': 'info'},
    'prompt_writing': {'label': 'Prompt Writing', 'token': 'primary'},
    'bbox_labeling':  {'label': 'BBox Labeling',  'token': 'warning'},
    'image_comparison': {'label': 'Image Comparison', 'token': 'neutral'},
    'text':           {'label': 'Text',           'token': 'neutral'},
    'coding':         {'label': 'Coding',         'token': 'neutral'},
    'image_text':     {'label': 'Image + Text',   'token': 'neutral'},
    'video':          {'label': 'Video',          'token': 'neutral'},
}

ERROR_CODES = (
    'INVALID_PAYLOAD', 'NOT_FOUND', 'FORBIDDEN',
    'NOT_LOCKED_YET', 'ALREADY_LOCKED', 'ALREADY_SENT',
    'REVIEW_PENDING', 'NO_CANDIDATES', 'MISSING_SCHEDULE',
    'START_IN_PAST', 'INVALID_STATE', 'POINTERS_OUT_OF_RANGE',
    'READ_ONLY', 'REGENERATING_IN_PROGRESS',
)


def coerce_int(value, default=0):
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ('true', '1', 'yes', 'y', 'on'):
        return True
    if text in ('false', '0', 'no', 'n', 'off'):
        return False
    return default


def paginate(params):
    page = max(1, coerce_int(params.get('page'), 1))
    limit = coerce_int(params.get('limit'), DEFAULT_LIMIT)
    if limit <= 0:
        limit = DEFAULT_LIMIT
    limit = min(limit, MAX_LIMIT)
    offset = (page - 1) * limit
    return page, limit, offset


def pagination_block(total, page, limit):
    pages = (total + limit - 1) // limit if limit else 1
    return {
        'total': total,
        'page': page,
        'limit': limit,
        'pages': max(pages, 1),
        'has_prev': page > 1,
        'has_next': page < pages,
    }


def iso_or_none(value):
    if not value:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def parse_json_body():
    raw = request.httprequest.get_data(as_text=True) if request and request.httprequest else ''
    if not raw:
        return dict(request.params or {})
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return dict(request.params or {})
    if not isinstance(body, dict):
        return dict(request.params or {})
    merged = dict(request.params or {})
    merged.update(body)
    return merged


def user_role_tag(env):
    user = env.user
    if user.has_group('etp_assessment.group_assessment_manager'):
        return 'manager'
    if user.has_group('etp_assessment.group_assessment_evaluator'):
        return 'evaluator'
    return None


def require_assessment_user():
    if user_role_tag(request.env) is None:
        return error_response('FORBIDDEN', 'Authentication required.', status=403)
    return None


def require_assessment_manager():
    if user_role_tag(request.env) != 'manager':
        return error_response('FORBIDDEN', 'Manager privileges required.', status=403)
    return None


def error_response(code, message, details=None, status=400):
    payload = {'code': code}
    if details:
        payload['details'] = details
    return return_Response(message=message, status=status, data=payload)


def manager_payload(blocks, **extras):
    data = {'role': 'manager', 'blocks': blocks}
    data.update(extras)
    return data


def type_pill(question_type):
    return TYPE_PILL.get(question_type) or {'label': (question_type or '').replace('_', ' ').title(), 'token': 'neutral'}


def m2o_link(rec):
    if not rec:
        return False
    return {'id': rec.id, 'name': rec.display_name or rec.name or ''}
