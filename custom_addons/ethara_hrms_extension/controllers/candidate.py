import base64
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    is_valid_email,
    is_valid_mobile,
    safe_get_value,
    generate_s3_link,
)
from odoo.addons.api_auth_gateway.controllers.main import validate_request

_logger = logging.getLogger(__name__)


ALLOWED_SORT_FIELDS = {'create_date', 'write_date', 'name', 'email'}
BOOL_FIELD_MAP = {
    'has_resume': 'resume_url',
    'has_linkedin': 'linkedin_url',
    'has_github': 'github_url',
    'has_portfolio': 'portfolio_url',
}
ALLOWED_APPLICANT_SORT_FIELDS = {
    'create_date', 'write_date', 'application_status', 'status_updated_at',
}
TERMINAL_APPLICATION_STATUSES = {'hired', 'refused', 'archived'}


def _parse_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ('1', 'true', 'yes', 'y'):
        return True
    if s in ('0', 'false', 'no', 'n'):
        return False
    return None


def _parse_int(v, default=None):
    if v is None or v == '':
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_datetime(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _iso_utc(dt):
    if not dt:
        return None
    if isinstance(dt, str):
        return dt
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'


def _serialize_candidate(candidate):
    return {
        'id': safe_get_value(candidate, 'id', 'int'),
        'name': safe_get_value(candidate, 'name', 'str'),
        'email': safe_get_value(candidate, 'email', 'str'),
        'mobile': safe_get_value(candidate, 'mobile', 'str'),
        'linkedinUrl': safe_get_value(candidate, 'linkedin_url', 'str'),
        'portfolioUrl': safe_get_value(candidate, 'portfolio_url', 'str'),
        'githubUrl': safe_get_value(candidate, 'github_url', 'str'),
        'resumeUrl': safe_get_value(candidate, 'resume_url', 'str'),
        'createdAt': _iso_utc(candidate.create_date),
        'updatedAt': _iso_utc(candidate.write_date),
    }


def _serialize_applicant(applicant):
    return {
        'id': safe_get_value(applicant, 'id', 'int'),
        'candidateId': safe_get_value(applicant, 'candidate_id.id', 'int'),
        'candidateName': safe_get_value(applicant, 'candidate_id.name', 'str'),
        'candidateEmail': safe_get_value(applicant, 'candidate_id.email', 'str'),
        'candidateMobile': safe_get_value(applicant, 'candidate_id.mobile', 'str'),
        'jobId': safe_get_value(applicant, 'job_id.id', 'int'),
        'jobTitle': safe_get_value(applicant, 'job_id.name', 'str'),
        'jobSlug': safe_get_value(applicant, 'job_id.slug', 'str'),
        'departmentId': safe_get_value(applicant, 'department_id.id', 'int'),
        'stageId': safe_get_value(applicant, 'stage_id.id', 'int'),
        'stage': safe_get_value(applicant, 'stage_id.name', 'str'),
        'applicationStatus': safe_get_value(applicant, 'application_status', 'str'),
        'isActive': safe_get_value(applicant, 'active', 'bool'),
        'linkedinUrl': safe_get_value(applicant, 'linkedin_profile', 'str'),
        'portfolioUrl': safe_get_value(applicant, 'portfolio_url', 'str'),
        'githubUrl': safe_get_value(applicant, 'github_url', 'str'),
        'resumeUrl': safe_get_value(applicant, 'resume_url', 'str'),
        'cancellationReason': safe_get_value(applicant, 'cancellation_reason', 'str'),
        'statusUpdatedAt': _iso_utc(applicant.status_updated_at),
        'refuseReason': safe_get_value(applicant, 'refuse_reason_id.name', 'str'),
        'appliedAt': _iso_utc(applicant.create_date),
        'createdAt': _iso_utc(applicant.create_date),
        'updatedAt': _iso_utc(applicant.write_date),
    }


def _find_active_applicant(candidate, job=None):
    domain = [
        ('candidate_id', '=', candidate.id),
        ('active', '=', True),
        ('application_status', '=', 'ongoing'),
    ]
    if job:
        domain.append(('job_id', '=', job.id))
    return request.env['hr.applicant'].sudo().search(domain, limit=1)


def _resolve_job(job_id=None, slug=None):
    Job = request.env['hr.job'].sudo()
    if job_id:
        job = Job.browse(job_id)
        return job if job.exists() else Job.browse()
    if slug:
        return Job.search([('slug', '=', slug)], limit=1)
    return Job.browse()


class EtharaCandidateController(http.Controller):

    @http.route('/api/v1/candidate/create', type='http', auth='none',
                methods=['POST'], csrf=False, cors='*')
    def create_candidate(self, **kwargs):
        try:
            name = (kwargs.get('name') or '').strip()
            email = (kwargs.get('email') or '').strip()
            mobile = (kwargs.get('mobile') or '').strip()

            if not name:
                return return_Response('Name is required', 400, errors=['name is required'])
            if not email or not is_valid_email(email):
                return return_Response('Valid email is required', 400, errors=['invalid email'])
            if not mobile or not is_valid_mobile(mobile):
                return return_Response('Valid mobile is required', 400, errors=['invalid mobile'])

            vals = {
                'name': name,
                'email': email,
                'mobile': mobile,
                'linkedin_url': (kwargs.get('linkedin_url') or '').strip() or False,
                'portfolio_url': (kwargs.get('portfolio_url') or '').strip() or False,
                'github_url': (kwargs.get('github_url') or '').strip() or False,
                'resume_url': (kwargs.get('resume_url') or '').strip() or False,
            }

            resume_file = request.httprequest.files.get('resume')
            if resume_file and resume_file.filename:
                raw = resume_file.read()
                if raw:
                    b64 = base64.b64encode(raw)
                    s3_url = generate_s3_link(
                        b64, prefix='candidate_resume',
                        filename=resume_file.filename,
                    )
                    if s3_url:
                        vals['resume_url'] = s3_url

            candidate = request.env['ethara.candidate'].sudo().create(vals)
            return return_Response(
                'Candidate created', 200,
                data={'record': _serialize_candidate(candidate)},
            )
        except Exception as exc:
            _logger.exception('create_candidate failed')
            return return_Response('Internal error', 500, errors=[str(exc)])

    @http.route('/api/v1/candidate/list', type='http', auth='none',
                methods=['GET', 'POST'], csrf=False, cors='*')
    @validate_request({})
    def list_candidates(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            params = {**kwargs, **jdata}

            domain = []
            search = (params.get('search') or '').strip()
            if search:
                domain += ['|', '|',
                          ('name', 'ilike', search),
                          ('email', 'ilike', search),
                          ('mobile', 'ilike', search)]

            name = (params.get('name') or '').strip()
            if name:
                domain.append(('name', 'ilike', name))

            email = (params.get('email') or '').strip()
            if email:
                domain.append(('email', '=', email))

            mobile = (params.get('mobile') or '').strip()
            if mobile:
                domain.append(('mobile', '=', mobile))

            for key, field in BOOL_FIELD_MAP.items():
                if key in params and params.get(key) not in (None, ''):
                    flag = _parse_bool(params.get(key))
                    if flag is True:
                        domain.append((field, '!=', False))
                    elif flag is False:
                        domain.append((field, '=', False))

            created_from = _parse_datetime(params.get('created_from'))
            if created_from:
                domain.append(('create_date', '>=', created_from))
            created_to = _parse_datetime(params.get('created_to'))
            if created_to:
                domain.append(('create_date', '<=', created_to))

            sort_by = params.get('sort_by') or 'create_date'
            if sort_by not in ALLOWED_SORT_FIELDS:
                sort_by = 'create_date'
            order_dir = 'asc' if (params.get('order') or 'desc').lower() == 'asc' else 'desc'
            order = '%s %s, id desc' % (sort_by, order_dir)

            page = max(_parse_int(params.get('page'), 1) or 1, 1)
            page_size = _parse_int(params.get('page_size'), 20) or 20
            page_size = max(1, min(page_size, 100))
            offset = (page - 1) * page_size

            Candidate = request.env['ethara.candidate'].sudo()
            total = Candidate.search_count(domain)
            recs = Candidate.search(domain, order=order, limit=page_size, offset=offset)
            total_pages = (total + page_size - 1) // page_size if page_size else 0

            return return_Response(
                'OK', 200,
                data={
                    'records': [_serialize_candidate(r) for r in recs],
                    'pagination': {
                        'page': page,
                        'page_size': page_size,
                        'total': total,
                        'total_pages': total_pages,
                        'has_next': page < total_pages,
                        'has_prev': page > 1,
                    },
                },
            )
        except Exception as exc:
            _logger.exception('list_candidates failed')
            return return_Response('Internal error', 500, errors=[str(exc)])
