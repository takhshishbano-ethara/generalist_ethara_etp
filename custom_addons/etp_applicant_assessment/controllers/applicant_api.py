import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
    validate_request,
    safe_get_value,
)
_logger = logging.getLogger(__name__)


LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100
LIST_BY_JOB_STATUS = 'resume_screening_passed'


def _iso_utc(dt):
    if not dt:
        return None
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'


def _coerce_int(value, default=None):
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _serialize_applicant_row(applicant):
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
        'status_updated_at': _iso_utc(applicant.status_updated_at),
        'active': safe_get_value(applicant, 'active', 'bool'),
        'created_at': _iso_utc(applicant.create_date),
        'updated_at': _iso_utc(applicant.write_date),
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

            domain = [
                ('job_id', '=', job.id),
                ('status', '=', LIST_BY_JOB_STATUS),
            ]

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

            return return_Response(
                'OK', 200,
                data={
                    'data': {
                        'job': {
                            'id': job.id,
                            'name': safe_get_value(job, 'name', 'str'),
                        },
                        'status_filter': [LIST_BY_JOB_STATUS],
                        'records': [_serialize_applicant_row(a) for a in recs],
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
