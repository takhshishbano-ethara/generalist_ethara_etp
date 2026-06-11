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
    manager_payload,
    require_assessment_manager,
)


def _format_date_label(value):
    if not value:
        return ''
    try:
        return value.strftime('%d %b')
    except AttributeError:
        return str(value)


def _summary_chip(parts):
    daily_window = ''
    if parts.get('daily_start_label') or parts.get('daily_end_label'):
        daily_window = '%s\u2013%s daily' % (
            parts.get('daily_start_label') or '00:00',
            parts.get('daily_end_label') or '00:00',
        )
    date_range = ''
    if parts.get('start_date') and parts.get('end_date'):
        date_range = '%s\u2013%s' % (
            _format_date_label(parts['start_date']),
            _format_date_label(parts['end_date']),
        )
    label_parts = [
        '%s Q' % parts.get('question_count', 0),
        '%s days' % parts.get('day_span', 0),
        '%s people' % parts.get('candidate_count', 0),
    ]
    if date_range:
        label_parts.append(date_range)
    if daily_window:
        label_parts.append(daily_window)
    label = ' \u00b7 '.join(label_parts)
    return {'label': label, 'parts': {
        'question_count': parts.get('question_count'),
        'day_span': parts.get('day_span'),
        'candidate_count': parts.get('candidate_count'),
        'start_date': iso_or_none(parts.get('start_date')),
        'end_date': iso_or_none(parts.get('end_date')),
        'daily_start_time': parts.get('daily_start_time'),
        'daily_end_time': parts.get('daily_end_time'),
        'daily_start_label': parts.get('daily_start_label'),
        'daily_end_label': parts.get('daily_end_label'),
    }}


class ModScheduleConfirmController(http.Controller):

    @http.route(
        '/api/v1/assessment_extension/assessments/<int:assessment_id>/send_preflight',
        type='http', auth='none', methods=['GET'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    @validate_request({})
    def send_preflight(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env['etp.assessment'].sudo().browse(assessment_id).exists()
        if not assessment:
            return error_response('NOT_FOUND', 'Assessment not found.', status=404)

        can_send, reasons = assessment._can_send()
        parts = assessment._compute_summary_parts()
        candidate_count = parts.get('candidate_count', 0)
        body_text = "Send this assessment to %s people?" % candidate_count
        if assessment.start_date:
            body_text += " They'll be notified and it goes live on %s." % _format_date_label(assessment.start_date.date())
        else:
            body_text += " They'll be notified and it goes live on the configured start date."

        cancel_action = {'key': 'cancel', 'label': 'Cancel', 'style': 'secondary'}
        send_action = {
            'key': 'send', 'label': 'Send assessment', 'style': 'success', 'icon': 'send',
            'method': 'POST',
            'href': '/api/v1/assessment_extension/assessments/%d/send' % assessment.id,
            'disabled': not can_send,
        }
        if not can_send and reasons:
            send_action['disabled_reason'] = reasons[0].get('message')

        modal = {
            'type': 'modal',
            'modal_id': 'MOD-Schedule-Confirm',
            'title': 'Send this assessment?',
            'body': body_text,
            'summary_chip': _summary_chip(parts),
            'callout': {
                'level': 'info',
                'token': 'neutral',
                'text': 'Once sent, candidates are notified and the schedule is live.',
            },
            'preflight': {
                'can_send': can_send,
                'reasons': reasons,
            },
            'actions': [cancel_action, send_action],
            'assessment': {
                'id': assessment.id,
                'name': assessment.name,
                'derived_state': assessment._compute_derived_state(),
                'questions_locked': assessment.questions_locked,
                'state': assessment.state,
            },
        }
        return return_Response(message='OK', status=200, data=manager_payload([modal]))

    @http.route(
        '/api/v1/assessment_extension/assessments/<int:assessment_id>/send',
        type='http', auth='none', methods=['POST'],
        csrf=False, cors='*', save_session=False,
    )
    @validate_token
    @validate_request({})
    def send_assessment(self, assessment_id, **kwargs):
        forbidden = require_assessment_manager()
        if forbidden is not None:
            return forbidden

        assessment = request.env['etp.assessment'].sudo().browse(assessment_id).exists()
        if not assessment:
            return error_response('NOT_FOUND', 'Assessment not found.', status=404)

        if assessment.state in ('in_progress', 'done'):
            return _send_result(assessment, outcome='noop_already_sent', emails_sent=0, tokens_issued=0)

        can_send, reasons = assessment._can_send()
        if not can_send:
            first = reasons[0] if reasons else {'code': 'INVALID_STATE'}
            return error_response(
                first.get('code') or 'INVALID_STATE',
                first.get('message') or _('Cannot send this assessment.'),
                details=first.get('details'),
                status=400,
            )

        try:
            with request.env.cr.savepoint():
                assessment.action_start()
        except (UserError, ValidationError) as exc:
            return error_response(
                'INVALID_STATE',
                str(exc.args[0] if exc.args else exc),
                status=400,
            )

        assessment.invalidate_recordset()
        evaluators_with_token = assessment.assessment_evaluator_ids.filtered(lambda e: bool(e.access_token))
        emails_sent = len(evaluators_with_token)
        tokens_issued = len(evaluators_with_token)
        return _send_result(
            assessment, outcome='sent',
            emails_sent=emails_sent, tokens_issued=tokens_issued,
        )


def _send_result(assessment, outcome, emails_sent, tokens_issued):
    parts = assessment._compute_summary_parts()
    evaluators = [
        {
            'id': ev.id,
            'employee_id': ev.employee_id.id if ev.employee_id else False,
            'name': ev.employee_id.name if ev.employee_id else '',
            'state': ev.state,
        }
        for ev in assessment.assessment_evaluator_ids
    ]
    payload = {
        'type': 'action_result',
        'outcome': outcome,
        'assessment': {
            'id': assessment.id,
            'name': assessment.name,
            'state': assessment.state,
            'derived_state': assessment._compute_derived_state(),
            'questions_locked': assessment.questions_locked,
            'start_date': iso_or_none(assessment.start_date),
            'end_date': iso_or_none(assessment.end_date),
            'daily_start_time': assessment.daily_start_time,
            'daily_end_time': assessment.daily_end_time,
            'candidate_count': parts.get('candidate_count'),
        },
        'dispatch': {
            'emails_sent': emails_sent,
            'tokens_issued': tokens_issued,
            'evaluators': evaluators,
        },
    }
    return return_Response(message='OK', status=200, data=manager_payload([payload]))
