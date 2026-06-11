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
    require_assessment_manager,
)


def _summary_chip(parts):
    by_type = parts.get('by_type') or {}
    label = '%s questions \u00b7 %s days \u00b7 %s Eval / %s Prompt / %s BBox' % (
        parts.get('question_count', 0),
        parts.get('day_span', 0),
        by_type.get('eval_compare', 0),
        by_type.get('prompt_writing', 0),
        by_type.get('bbox_labeling', 0),
    )
    return {'label': label, 'parts': parts}


def _reasons_to_message(reasons):
    if not reasons:
        return _('Cannot lock this assessment.')
    return reasons[0].get('message') or _('Cannot lock this assessment.')


class ModLockConfirmController(http.Controller):

    @http.route(
        '/api/v1/assessment_extension/assessments/<int:assessment_id>/lock_preflight',
        type='http', auth='none', methods=['GET'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    @validate_request({})
    def lock_preflight(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env['etp.assessment'].sudo().browse(assessment_id).exists()
        if not assessment:
            return error_response('NOT_FOUND', 'Assessment not found.', status=404)

        assessment._sync_question_reviews()
        can_lock, reasons = assessment._can_lock()
        parts = assessment._compute_summary_parts()

        cancel_action = {'key': 'cancel', 'label': 'Cancel', 'style': 'secondary'}
        lock_action = {
            'key': 'lock', 'label': 'Lock questions', 'style': 'primary', 'icon': 'lock',
            'method': 'POST',
            'href': '/api/v1/assessment_extension/assessments/%d/lock' % assessment.id,
            'disabled': not can_lock,
        }
        if not can_lock and reasons:
            lock_action['disabled_reason'] = reasons[0].get('message')

        modal = {
            'type': 'modal',
            'modal_id': 'MOD-Lock-Confirm',
            'title': 'Lock this assessment?',
            'body': (
                "All %s questions and their answers become permanent. "
                "You'll then assign people and schedule. This can't be undone."
            ) % parts.get('question_count', 0),
            'summary_chip': _summary_chip(parts),
            'callout': {
                'level': 'warning',
                'token': 'warning',
                'text': 'Locking makes every question and its answers permanent.',
            },
            'preflight': {
                'can_lock': can_lock,
                'reasons': reasons,
            },
            'actions': [cancel_action, lock_action],
            'assessment': {
                'id': assessment.id,
                'name': assessment.name,
                'derived_state': assessment._compute_derived_state(),
                'questions_locked': assessment.questions_locked,
                'pending_review_count': assessment.pending_review_count,
                'total_questions': len(assessment.question_ids),
            },
        }
        return return_Response(message='OK', status=200, data=manager_payload([modal]))

    @http.route(
        '/api/v1/assessment_extension/assessments/<int:assessment_id>/lock',
        type='http', auth='none', methods=['POST'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    @validate_request({})
    def lock_assessment(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env['etp.assessment'].sudo().browse(assessment_id).exists()
        if not assessment:
            return error_response('NOT_FOUND', 'Assessment not found.', status=404)

        if assessment.questions_locked:
            return _lock_result(assessment, outcome='noop_already_locked')

        assessment._sync_question_reviews()
        can_lock, reasons = assessment._can_lock()
        if not can_lock:
            first = reasons[0] if reasons else {'code': 'INVALID_STATE'}
            return error_response(
                first.get('code') or 'INVALID_STATE',
                first.get('message') or _reasons_to_message(reasons),
                details=first.get('details'),
                status=400,
            )

        try:
            with request.env.cr.savepoint():
                assessment._stamp_questions_locked(user=request.env.user)
        except (UserError, ValidationError) as exc:
            return error_response(
                'INVALID_STATE',
                str(exc.args[0] if exc.args else exc),
                status=400,
            )

        return _lock_result(assessment, outcome='locked')


def _lock_result(assessment, outcome):
    parts = assessment._compute_summary_parts()
    payload = {
        'type': 'action_result',
        'outcome': outcome,
        'assessment': {
            'id': assessment.id,
            'name': assessment.name,
            'questions_locked': assessment.questions_locked,
            'questions_locked_at': iso_or_none(assessment.questions_locked_at),
            'questions_locked_by': m2o_link(assessment.questions_locked_by_id),
            'derived_state': assessment._compute_derived_state(),
            'total_questions': parts.get('question_count'),
            'day_span': parts.get('day_span'),
            'by_type': parts.get('by_type'),
        },
        'next_step': {'key': 'assign', 'label': 'Assign candidates'},
    }
    return return_Response(message='OK', status=200, data=manager_payload([payload]))
