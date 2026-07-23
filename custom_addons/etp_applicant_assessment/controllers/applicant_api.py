import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
    safe_get_value,
)
from odoo.addons.etp_applicant_assessment.models.hr_applicant import (
    HIRING_STATUS_KEYS,
)
from odoo.addons.etp_applicant_assessment.models._common import to_ist

_logger = logging.getLogger(__name__)


LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100
LIST_BY_JOB_STATUS = 'resume_screening_passed'

# Assessment states in which the invitation has actually been dispatched to
# the candidate. A record leaves 'draft' the moment action_send() emails the
# portal link, so anything at/after 'sent' counts as "invitation sent".
# 'cancelled' is deliberately excluded: the invite is no longer active.
_INVITATION_SENT_STATES = ('sent', 'in_progress', 'submitted', 'scored')
_INVITATION_FILTER_KEYS = ('sent', 'not_sent', 'all')


def _iso_ist(dt):
    if not dt:
        return None
    return to_ist(dt).strftime('%Y-%m-%dT%H:%M:%S.%f') + '+05:30'


def _coerce_int(value, default=None):
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _serialize_applicant_assessment(assessment):
    """Compact view of the candidate's most recent assessment for the job,
    or None when no assessment record exists yet."""
    if not assessment:
        return None
    return {
        'id': assessment.id,
        'state': assessment.state,
        'result': assessment.result,
        'sent_at': _iso_ist(assessment.create_date),
        'started_at': _iso_ist(assessment.started_at),
        'submitted_at': _iso_ist(assessment.submitted_at),
        'final_score': assessment.final_score,
        'test_link': safe_get_value(assessment, 'portal_url', 'str'),
    }


def _serialize_applicant_row(applicant, assessment_info=None):
    info = assessment_info or {}
    invitation_sent = bool(info.get('sent'))
    return {
        'id': applicant.id,
        'name': safe_get_value(applicant, 'partner_name', 'str'),
        'email': safe_get_value(applicant, 'email_from', 'str'),
        'mobile': safe_get_value(applicant, 'partner_phone', 'str'),
        'job_id': safe_get_value(applicant, 'job_id.id', 'int'),
        'job_title': safe_get_value(applicant, 'job_id.name', 'str'),
        'candidate_id': safe_get_value(applicant, 'candidate_id.id', 'int'),
        'candidate_name': safe_get_value(applicant, 'candidate_id.name', 'str'),
        'stage_id': safe_get_value(applicant, 'stage_id.id', 'int'),
        'stage': safe_get_value(applicant, 'stage_id.name', 'str'),
        'status': safe_get_value(applicant, 'status', 'str'),
        'status_updated_at': _iso_ist(applicant.status_updated_at),
        'active': safe_get_value(applicant, 'active', 'bool'),
        'created_at': _iso_ist(applicant.create_date),
        'updated_at': _iso_ist(applicant.write_date),
        # Whether the assessment invitation/link has been dispatched to this
        # candidate (derived from their assessment records for the job).
        'invitation_sent': invitation_sent,
        'invitation_status': 'sent' if invitation_sent else 'not_sent',
        'assessment': _serialize_applicant_assessment(info.get('latest')),
    }


class ApplicantAssessmentApplicantApi(http.Controller):

    @http.route(
        '/api/v1/applicant/list-by-job', type='http', auth='none',
        methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    @validate_request({})
    def list_applicants_by_job(self, jdata=None, **kwargs):
        try:
            data = jdata or {}

            job_id = _coerce_int(data.get('job_id'))
            if not job_id:
                return return_Response(
                    'job_id is required', 400,
                    errors=['job_id is required'],
                )

            job = request.env['hr.job'].sudo().browse(job_id)
            if not job.exists():
                return return_Response(
                    'Job not found', 404,
                    errors=['job_id %s not found' % job_id],
                )

            Assessment = request.env['etp.applicant.assessment'].sudo()
            template_id = _coerce_int(data.get('template_id'))

            domain = [('job_id', '=', job.id)]

            # Restrict to candidates the invitation has / hasn't been sent to.
            # Values: 'sent', 'not_sent', 'all' (default = no restriction).
            invitation_filter = (
                data.get('invitation_status') or ''
            ).strip().lower()
            if (
                invitation_filter
                and invitation_filter not in _INVITATION_FILTER_KEYS
            ):
                return return_Response(
                    'Invalid invitation_status %r' % invitation_filter, 400,
                    errors=[
                        'invitation_status must be one of: %s'
                        % ', '.join(_INVITATION_FILTER_KEYS)
                    ],
                )

            # Applicant hiring-status filter. Defaults to
            # 'resume_screening_passed' (candidates eligible to be invited).
            # Pass an explicit `status` (string or list) to override, or 'all'
            # to drop the constraint. When asking for already-invited
            # candidates we relax it automatically, since sending an invite
            # advances the applicant past 'resume_screening_passed'.
            status_param = data.get('status')
            if isinstance(status_param, str) and status_param.strip():
                s = status_param.strip()
                if s.lower() == 'all':
                    status_applied = []
                elif s in HIRING_STATUS_KEYS:
                    domain.append(('status', '=', s))
                    status_applied = [s]
                else:
                    return return_Response(
                        'Invalid status %r' % s, 400,
                        errors=['status is not a valid hiring status'],
                    )
            elif isinstance(status_param, list) and status_param:
                for s in status_param:
                    if s not in HIRING_STATUS_KEYS:
                        return return_Response(
                            'Invalid status %r' % s, 400,
                            errors=['status is not a valid hiring status'],
                        )
                domain.append(('status', 'in', status_param))
                status_applied = list(status_param)
            elif invitation_filter == 'sent':
                status_applied = []
            else:
                domain.append(('status', '=', LIST_BY_JOB_STATUS))
                status_applied = [LIST_BY_JOB_STATUS]

            # Restrict by invitation state using the ground-truth assessment
            # records (scoped to this job, optionally a specific template).
            if invitation_filter in ('sent', 'not_sent'):
                invite_domain = [
                    ('job_id', '=', job.id),
                    ('state', 'in', list(_INVITATION_SENT_STATES)),
                ]
                if template_id:
                    invite_domain.append(('template_id', '=', template_id))
                invited_ids = list(set(
                    Assessment.search(invite_domain)
                    .mapped('applicant_id').ids
                ))
                domain.append((
                    'id',
                    'in' if invitation_filter == 'sent' else 'not in',
                    invited_ids,
                ))

            search = (data.get('search') or '').strip()
            if search:
                domain += [
                    '|',
                    ('partner_name', 'ilike', search),
                    ('email_from', 'ilike', search),
                ]

            page = max(1, _coerce_int(data.get('page'), 1))
            per_page = _coerce_int(data.get('limit'), LIST_DEFAULT_LIMIT)
            per_page = max(1, min(per_page, LIST_MAX_LIMIT))
            offset = (page - 1) * per_page

            Applicant = request.env['hr.applicant'].sudo()
            total = Applicant.search_count(domain)
            recs = Applicant.search(
                domain, order='create_date desc, id desc',
                limit=per_page, offset=offset,
            )
            total_pages = (total + per_page - 1) // per_page if per_page else 0

            # Batch-load each candidate's assessments for this job so the
            # per-row invitation status costs no extra query per applicant.
            info_by_applicant = {}
            if recs:
                page_domain = [
                    ('applicant_id', 'in', recs.ids),
                    ('job_id', '=', job.id),
                ]
                if template_id:
                    page_domain.append(('template_id', '=', template_id))
                for a in Assessment.search(
                    page_domain, order='create_date desc',
                ):
                    info = info_by_applicant.setdefault(
                        a.applicant_id.id, {'latest': a, 'sent': False},
                    )
                    if a.state in _INVITATION_SENT_STATES:
                        info['sent'] = True

            return return_Response(
                'OK', 200,
                data={
                    'data': {
                        'job': {
                            'id': job.id,
                            'name': safe_get_value(job, 'name', 'str'),
                        },
                        'status_filter': status_applied,
                        'invitation_filter': invitation_filter or 'all',
                        'records': [
                            _serialize_applicant_row(
                                a, info_by_applicant.get(a.id),
                            )
                            for a in recs
                        ],
                    },
                    'pagination': {
                        'current_page': page,
                        'per_page': per_page,
                        'total': total,
                        'total_pages': total_pages,
                    },
                },
            )
        except Exception as exc:
            _logger.exception('list_applicants_by_job failed')
            return return_Response('Internal error', 500, errors=[str(exc)])

    @http.route(
        '/api/v1/applicant/update-status', type='http', auth='none',
        methods=['POST'], csrf=False, cors='*',
    )
    @validate_token
    @validate_request({})
    def update_applicant_status(self, jdata=None, **kwargs):
        try:
            data = jdata or {}

            candidate_id = _coerce_int(data.get('candidate_id'))
            if not candidate_id:
                return return_Response(
                    'candidate_id is required', 400,
                    errors=['candidate_id is required'],
                )

            status = data.get('status')
            status = status.strip() if isinstance(status, str) else ''
            if not status:
                return return_Response(
                    'status is required', 400,
                    errors=['status (string) is required'],
                )
            if status not in HIRING_STATUS_KEYS:
                return return_Response(
                    'Invalid status %r' % status, 400,
                    errors=[
                        'status must be one of: %s'
                        % ', '.join(sorted(HIRING_STATUS_KEYS))
                    ],
                )

            domain = [('id', '=', candidate_id)]

            applicant = request.env['hr.applicant'].sudo().search(
                domain, order='create_date desc, id desc', limit=1
            )
            if not applicant:
                return return_Response(
                    'No application found for this candidate', 404
                )
            
            previous_status = applicant.status
            changed = previous_status != status
            if changed:
                applicant.write({'status': status})

            return return_Response(
                'Status updated' if changed else 'Status unchanged', 200,
                data={
                    'data': {
                        'applicant_id': applicant.id,
                        'candidate_name': safe_get_value(
                            applicant, 'name', 'str',
                        ),
                        'previous_status': previous_status,
                        'status': applicant.status,
                        'status_updated_at': _iso_ist(
                            applicant.status_updated_at,
                        ),
                    },
                },
            )
        except Exception as exc:
            _logger.exception('update_applicant_status failed')
            return return_Response('Internal error', 500, errors=[str(exc)])
