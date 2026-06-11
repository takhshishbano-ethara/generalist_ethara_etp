from odoo import _, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_request,
    validate_token,
)

from .common import (
    error_response,
    iso_or_none,
    m2o_link,
    manager_payload,
    parse_json_body,
    require_assessment_manager,
    type_pill,
)


_EVAL_DIMS = (
    ('instruction_following', 'Instruction following'),
    ('visual_quality',        'Visual Quality'),
    ('less_ai_generated',     'Less AI-Generated'),
    ('overall',               'Overall'),
)
_EVAL_DIM_KEYS = tuple(k for k, _label in _EVAL_DIMS)
_POINTERS_MIN = 8
_POINTERS_MAX = 10


def _image_payload(question, field, label):
    url_field = field + '_url'
    binary_url = '/web/image/etp.assessment.question/%d/%s' % (question.id, field)
    return {
        'label': label,
        'url': getattr(question, url_field, '') or (binary_url if getattr(question, field, False) else ''),
        'has_image': bool(getattr(question, field, False) or getattr(question, url_field, '')),
    }


def _eval_compare_variant(question, correct, wrong):
    correct_payload = (correct.payload_json or {}) if correct else {}
    wrong_payload = (wrong.payload_json or {}) if wrong else {}
    return {
        'prompt': question.prompt or '',
        'images': {
            'response_a': _image_payload(question, 'image_a', 'Response A'),
            'response_b': _image_payload(question, 'image_b', 'Response B'),
        },
        'dimensions': [{'key': k, 'label': label} for k, label in _EVAL_DIMS],
        'correct_answer': {
            'label': 'Correct answer',
            'picks': correct_payload.get('picks', {}) or {},
            'justification': correct_payload.get('justification', '') or '',
        },
        'wrong_answer': {
            'label': 'Wrong answer (distractor)',
            'picks': wrong_payload.get('picks', {}) or {},
        },
    }


def _prompt_writing_variant(question, correct, wrong):
    correct_payload = (correct.payload_json or {}) if correct else {}
    wrong_payload = (wrong.payload_json or {}) if wrong else {}
    pointers = [{'id': p.id, 'sequence': p.sequence, 'text': p.text} for p in question.pointer_ids]
    pointers_total = len(pointers)
    return {
        'images': {
            'reference': _image_payload(question, 'image_a', 'Reference image'),
            'output_target': _image_payload(question, 'image_b', 'Output Target Image'),
        },
        'correct_answer': {
            'label': 'Correct answer',
            'golden_prompt': correct_payload.get('golden_prompt', '') or '',
            'pointers_label': 'Pointers (%d required)' % pointers_total if pointers_total else 'Pointers (10 required)',
            'pointers': pointers,
            'pointers_total': pointers_total,
            'pointers_min': _POINTERS_MIN,
            'pointers_max': _POINTERS_MAX,
        },
        'wrong_answer': {
            'label': 'Wrong answer (thin prompt)',
            'thin_prompt': wrong_payload.get('thin_prompt', '') or '',
        },
    }


def _bbox_variant(question, correct, wrong):
    return {
        'images': {
            'screenshot': _image_payload(question, 'image_a', 'UI Screenshot'),
        },
        'correct_answer': {
            'label': 'Correct answer',
            'payload': (correct.payload_json or {}) if correct else {},
        },
        'wrong_answer': {
            'label': 'Wrong answer (degraded)',
            'payload': (wrong.payload_json or {}) if wrong else {},
        },
    }


def _build_variant(question):
    correct = question._get_correct_answer()
    wrong = question._get_wrong_answer()
    if question.question_type == 'eval_compare':
        return _eval_compare_variant(question, correct, wrong)
    if question.question_type == 'prompt_writing':
        return _prompt_writing_variant(question, correct, wrong)
    if question.question_type == 'bbox_labeling':
        return _bbox_variant(question, correct, wrong)
    return {
        'prompt': question.prompt or '',
        'correct_answer': {'label': 'Correct answer', 'payload': (correct.payload_json or {}) if correct else {}},
        'wrong_answer': {'label': 'Wrong answer', 'payload': (wrong.payload_json or {}) if wrong else {}},
    }


def _footer_actions(review):
    locked = review.assessment_id.questions_locked
    is_approved = review.review_state == 'approved'
    is_regenerating = review.review_state == 'regenerating'

    base_url = '/api/v1/assessment_extension/review_question/%d' % review.id
    regenerate = {
        'key': 'regenerate', 'label': 'Regenerate', 'style': 'secondary', 'icon': 'refresh-cw',
        'method': 'POST', 'href': base_url + '/regenerate',
        'disabled': locked or is_regenerating,
    }
    edit = {
        'key': 'edit', 'label': 'Edit', 'style': 'secondary', 'icon': 'pencil',
        'method': 'PATCH', 'href': base_url,
        'disabled': locked or is_regenerating,
    }
    if is_approved and not locked:
        approve = {
            'key': 'approve', 'label': 'Approved', 'style': 'success-muted', 'icon': 'check',
            'method': 'POST', 'href': base_url + '/approve',
            'disabled': True,
        }
    else:
        approve = {
            'key': 'approve', 'label': 'Approve', 'style': 'success', 'icon': 'check',
            'method': 'POST', 'href': base_url + '/approve',
            'disabled': locked or is_regenerating,
        }
    if locked:
        for action in (regenerate, edit, approve):
            action['disabled_reason'] = 'Assessment is locked.'
    return [regenerate, edit, approve]


def _serialize_review(review):
    question = review.question_id
    pill = type_pill(question.question_type)
    return {
        'review': {
            'id': review.id,
            'review_state': review.review_state,
            'question_code': review._to_question_code(),
            'day_number': review.day_number,
            'day_sequence': review.day_sequence,
            'approved_by': m2o_link(review.approved_by_id) if review.approved_by_id else False,
            'approved_at': iso_or_none(review.approved_at),
        },
        'question': {
            'id': question.id,
            'name': question.name,
            'question_type': question.question_type,
            'type_label': pill['label'],
            'type_token': pill['token'],
        },
        'assessment': {
            'id': review.assessment_id.id,
            'name': review.assessment_id.name,
            'questions_locked': review.assessment_id.questions_locked,
            'pending_review_count': review.assessment_id.pending_review_count,
            'total_questions': len(review.assessment_id.question_ids),
        },
    }


def _drawer_block(review):
    question = review.question_id
    pill = type_pill(question.question_type)
    base = _serialize_review(review)
    locked = review.assessment_id.questions_locked
    drawer = {
        'type': 'drawer',
        'modal_id': 'MOD-Review-Question',
        'title': 'Review question · %s' % review._to_question_code(),
        'type_pill': pill,
        'question_code': review._to_question_code(),
        'question_type': question.question_type,
        'review_state': review.review_state,
        'assessment': base['assessment'],
        'question': base['question'],
        'variant_payload': _build_variant(question),
        'footer': {
            'actions': _footer_actions(review),
            'can_modify': not locked,
        },
    }
    return drawer


def _approved_count(assessment):
    return len(assessment.question_review_ids.filtered(lambda r: r.review_state == 'approved'))


def _assessment_progress(assessment):
    total = len(assessment.question_ids)
    approved = _approved_count(assessment)
    return {
        'id': assessment.id,
        'name': assessment.name,
        'pending_review_count': max(0, total - approved),
        'total_questions': total,
        'approved_count': approved,
    }


def _validate_eval_payload(payload):
    if 'prompt' in payload and not isinstance(payload['prompt'], str):
        return 'prompt must be a string'
    for role in ('correct_answer', 'wrong_answer'):
        section = payload.get(role)
        if section is None:
            continue
        if not isinstance(section, dict):
            return '%s must be an object' % role
        picks = section.get('picks')
        if picks is None and role == 'wrong_answer' and not section:
            continue
        if picks is not None:
            if not isinstance(picks, dict):
                return '%s.picks must be an object' % role
            for key, val in picks.items():
                if key not in _EVAL_DIM_KEYS:
                    return '%s.picks has unknown dimension %r' % (role, key)
                if val not in ('A', 'B'):
                    return '%s.picks.%s must be "A" or "B"' % (role, key)
        if role == 'correct_answer' and 'justification' in section:
            if not isinstance(section['justification'], str):
                return 'correct_answer.justification must be a string'
    return None


def _validate_prompt_payload(payload):
    correct = payload.get('correct_answer')
    if correct is not None:
        if not isinstance(correct, dict):
            return 'correct_answer must be an object'
        if 'golden_prompt' in correct and not isinstance(correct['golden_prompt'], str):
            return 'correct_answer.golden_prompt must be a string'
        pointers = correct.get('pointers')
        if pointers is not None:
            if not isinstance(pointers, list):
                return 'correct_answer.pointers must be a list'
            for idx, item in enumerate(pointers):
                if not isinstance(item, dict):
                    return 'correct_answer.pointers[%d] must be an object' % idx
                if 'text' not in item or not isinstance(item['text'], str) or not item['text'].strip():
                    return 'correct_answer.pointers[%d].text must be a non-empty string' % idx
    wrong = payload.get('wrong_answer')
    if wrong is not None:
        if not isinstance(wrong, dict):
            return 'wrong_answer must be an object'
        if 'thin_prompt' in wrong and not isinstance(wrong['thin_prompt'], str):
            return 'wrong_answer.thin_prompt must be a string'
    return None


def _upsert_answer(env, question, role, payload_json):
    Answer = env['etp.assessment.question.answer'].sudo()
    existing = Answer.search([('question_id', '=', question.id), ('answer_role', '=', role)], limit=1)
    if existing:
        existing.write({'payload_json': payload_json})
        return existing
    return Answer.create({
        'question_id': question.id,
        'answer_role': role,
        'payload_json': payload_json,
    })


def _apply_eval_patch(env, question, payload):
    if 'prompt' in payload:
        question.write({'prompt': payload['prompt']})
    correct = payload.get('correct_answer')
    if isinstance(correct, dict):
        existing = question._get_correct_answer()
        merged = dict(existing.payload_json or {}) if existing else {}
        if 'picks' in correct:
            merged['picks'] = correct['picks']
        if 'justification' in correct:
            merged['justification'] = correct['justification']
        _upsert_answer(env, question, 'correct', merged)
    wrong = payload.get('wrong_answer')
    if isinstance(wrong, dict):
        existing = question._get_wrong_answer()
        merged = dict(existing.payload_json or {}) if existing else {}
        if 'picks' in wrong:
            merged['picks'] = wrong['picks']
        _upsert_answer(env, question, 'wrong', merged)


def _apply_prompt_patch(env, question, payload):
    correct = payload.get('correct_answer')
    if isinstance(correct, dict):
        existing = question._get_correct_answer()
        merged = dict(existing.payload_json or {}) if existing else {}
        if 'golden_prompt' in correct:
            merged['golden_prompt'] = correct['golden_prompt']
        _upsert_answer(env, question, 'correct', merged)
        if 'pointers' in correct:
            pointers = correct['pointers']
            if not (_POINTERS_MIN <= len(pointers) <= _POINTERS_MAX):
                raise UserError(_(
                    'Prompt Writing requires between %d and %d pointers (got %d).'
                ) % (_POINTERS_MIN, _POINTERS_MAX, len(pointers)))
            question.pointer_ids.unlink()
            Pointer = env['etp.assessment.question.pointer'].sudo()
            for idx, item in enumerate(pointers):
                Pointer.create({
                    'question_id': question.id,
                    'sequence': item.get('sequence') or (idx + 1) * 10,
                    'text': item['text'],
                })
    wrong = payload.get('wrong_answer')
    if isinstance(wrong, dict):
        existing = question._get_wrong_answer()
        merged = dict(existing.payload_json or {}) if existing else {}
        if 'thin_prompt' in wrong:
            merged['thin_prompt'] = wrong['thin_prompt']
        _upsert_answer(env, question, 'wrong', merged)


class ModReviewQuestionController(http.Controller):

    @http.route(
        '/api/v1/assessment_extension/review_question/<int:review_id>',
        type='http', auth='none', methods=['GET'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    @validate_request({})
    def get_review_question(self, review_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        review = request.env['etp.assessment.question.review'].sudo().browse(review_id).exists()
        if not review:
            return error_response('NOT_FOUND', 'Review row not found.', status=404)

        drawer = _drawer_block(review)
        return return_Response(message='OK', status=200, data=manager_payload([drawer]))

    @http.route(
        '/api/v1/assessment_extension/review_question/<int:review_id>/approve',
        type='http', auth='none', methods=['POST'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    @validate_request({})
    def approve_review_question(self, review_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        review = request.env['etp.assessment.question.review'].sudo().browse(review_id).exists()
        if not review:
            return error_response('NOT_FOUND', 'Review row not found.', status=404)

        if review.assessment_id.questions_locked:
            return error_response('ALREADY_LOCKED', 'Assessment is locked; cannot approve.', status=400)

        if review.review_state == 'approved':
            return _action_result(review, outcome='noop_already_approved')

        if review.review_state == 'regenerating':
            return error_response(
                'INVALID_STATE',
                'Cannot approve a question that is being regenerated.',
                status=400,
            )

        try:
            with request.env.cr.savepoint():
                review._set_state('approved', by_user=request.env.user)
        except (UserError, ValidationError) as exc:
            return error_response('INVALID_STATE', str(exc.args[0] if exc.args else exc), status=400)

        return _action_result(review, outcome='approved')

    @http.route(
        '/api/v1/assessment_extension/review_question/<int:review_id>/regenerate',
        type='http', auth='none', methods=['POST'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    def regenerate_review_question(self, review_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        review = request.env['etp.assessment.question.review'].sudo().browse(review_id).exists()
        if not review:
            return error_response('NOT_FOUND', 'Review row not found.', status=404)

        if review.assessment_id.questions_locked:
            return error_response('ALREADY_LOCKED', 'Assessment is locked; cannot regenerate.', status=400)

        if review.review_state == 'regenerating':
            return error_response(
                'REGENERATING_IN_PROGRESS',
                'A regeneration is already in progress for this question.',
                status=400,
            )

        body = parse_json_body() or {}
        reason = body.get('reason') or ''
        if reason and (not isinstance(reason, str) or len(reason) > 500):
            return error_response('INVALID_PAYLOAD', 'reason must be a string up to 500 characters.', status=400)

        try:
            with request.env.cr.savepoint():
                review._set_state('regenerating')
        except (UserError, ValidationError) as exc:
            return error_response('INVALID_STATE', str(exc.args[0] if exc.args else exc), status=400)

        payload = {
            'type': 'action_result',
            'outcome': 'regenerating_queued',
            'review': {
                'id': review.id,
                'review_state': review.review_state,
                'question_code': review._to_question_code(),
            },
            'note': 'Regeneration pipeline is not yet wired; state has been flipped to "regenerating".',
        }
        return return_Response(message='OK', status=200, data=manager_payload([payload]))

    @http.route(
        '/api/v1/assessment_extension/review_question/<int:review_id>',
        type='http', auth='none', methods=['PATCH'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    def patch_review_question(self, review_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        review = request.env['etp.assessment.question.review'].sudo().browse(review_id).exists()
        if not review:
            return error_response('NOT_FOUND', 'Review row not found.', status=404)

        if review.assessment_id.questions_locked:
            return error_response('ALREADY_LOCKED', 'Assessment is locked; cannot edit.', status=400)

        body = parse_json_body() or {}
        if not isinstance(body, dict) or not body:
            return error_response('INVALID_PAYLOAD', 'Request body must be a non-empty object.', status=400)

        question = review.question_id
        qtype = question.question_type
        if qtype == 'eval_compare':
            err = _validate_eval_payload(body)
        elif qtype == 'prompt_writing':
            err = _validate_prompt_payload(body)
        else:
            return error_response(
                'INVALID_STATE',
                'PATCH is not supported for question_type=%s.' % qtype,
                status=400,
            )
        if err:
            return error_response('INVALID_PAYLOAD', err, status=400)

        try:
            with request.env.cr.savepoint():
                if qtype == 'eval_compare':
                    _apply_eval_patch(request.env, question, body)
                else:
                    _apply_prompt_patch(request.env, question, body)
                review._set_state('draft')
        except UserError as exc:
            message = str(exc.args[0] if exc.args else exc)
            if 'pointers' in message.lower():
                count = len(question.pointer_ids)
                return error_response(
                    'POINTERS_OUT_OF_RANGE',
                    message,
                    details={'count': count, 'min': _POINTERS_MIN, 'max': _POINTERS_MAX},
                    status=400,
                )
            return error_response('INVALID_PAYLOAD', message, status=400)
        except ValidationError as exc:
            return error_response('INVALID_PAYLOAD', str(exc.args[0] if exc.args else exc), status=400)

        payload = {
            'type': 'action_result',
            'outcome': 'patched',
            'review': {
                'id': review.id,
                'review_state': review.review_state,
                'question_code': review._to_question_code(),
                'question_type': question.question_type,
            },
        }
        return return_Response(message='OK', status=200, data=manager_payload([payload]))

    @http.route(
        '/api/v1/assessment_extension/assessments/<int:assessment_id>/approve_all',
        type='http', auth='none', methods=['POST'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    @validate_request({})
    def approve_all(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env['etp.assessment'].sudo().browse(assessment_id).exists()
        if not assessment:
            return error_response('NOT_FOUND', 'Assessment not found.', status=404)

        if assessment.questions_locked:
            return error_response('ALREADY_LOCKED', 'Assessment is locked; cannot approve.', status=400)

        assessment._sync_question_reviews()
        drafts = assessment.question_review_ids.filtered(lambda r: r.review_state == 'draft')
        regenerating = assessment.question_review_ids.filtered(lambda r: r.review_state == 'regenerating')

        if not drafts:
            payload = {
                'type': 'action_result',
                'outcome': 'noop_all_already_approved',
                'approved_count': 0,
                'skipped_regenerating_count': len(regenerating),
                'assessment': _assessment_progress(assessment),
            }
            return return_Response(message='OK', status=200, data=manager_payload([payload]))

        approved_count = 0
        try:
            with request.env.cr.savepoint():
                for review in drafts:
                    review._set_state('approved', by_user=request.env.user)
                    approved_count += 1
        except (UserError, ValidationError) as exc:
            return error_response('INVALID_STATE', str(exc.args[0] if exc.args else exc), status=400)

        payload = {
            'type': 'action_result',
            'outcome': 'approved',
            'approved_count': approved_count,
            'skipped_regenerating_count': len(regenerating),
            'assessment': _assessment_progress(assessment),
        }
        return return_Response(message='OK', status=200, data=manager_payload([payload]))


def _action_result(review, outcome):
    base = _serialize_review(review)
    progress = _assessment_progress(review.assessment_id)
    payload = {
        'type': 'action_result',
        'outcome': outcome,
        'review': base['review'],
        'question': base['question'],
        'assessment': progress,
    }
    return return_Response(message='OK', status=200, data=manager_payload([payload]))
