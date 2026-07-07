import logging
from datetime import datetime

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    validate_request,
    return_Response,
)

_logger = logging.getLogger(__name__)

ALLOWED_SORT_FIELDS = {
    'posted_at',
    'create_date',
    'write_date',
    'name',
    'urgency_level',
    'no_of_recruitment',
}


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y', 't')
    return False


def _parse_int(value, default=None):
    try:
        if value in (None, '', False):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        value = value.strip()
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _iso_utc(dt):
    if not dt:
        return None
    if isinstance(dt, datetime):
        return dt.strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'
    return None


def _selection_label(record, field_name):
    value = getattr(record, field_name, None)
    if not value:
        return None
    try:
        selection = dict(record._fields[field_name]._description_selection(record.env))
        return selection.get(value, value)
    except Exception:
        return value


def _split_lines(text):
    if not text:
        return []
    parts = [p.strip() for p in text.splitlines()]
    return [p for p in parts if p]


def _serialize_job(job, detail=False):
    data = {
        'id': job.id,
        'title': job.name or '',
        'slug': job.slug or None,
        'department': job.department_id.name if job.department_id else None,
        'departmentId': job.department_id.id if job.department_id else None,
        'summary': job.summary or None,
        'location': job.job_location or None,
        'employmentType': _selection_label(job, 'employment_type'),
        'workMode': _selection_label(job, 'work_mode'),
        'experienceLevel': _selection_label(job, 'experience_level'),
        'experienceYears': job.experience_years or 0,
        'salaryBracket': job.salary_bracket or None,
        'featured': bool(job.is_featured),
        'openings': job.no_of_recruitment or 0,
        'postedAt': _iso_utc(job.posted_at),
        'urgencyLevel': int(job.urgency_level) if job.urgency_level else None,
        'isActive': bool(job.active),
        'approvalStatus': job.approval_status or None,
        'createdAt': _iso_utc(job.create_date),
        'updatedAt': _iso_utc(job.write_date),
    }

    if detail:
        data.update({
            'description': job.description or None,
            'responsibilities': [
                r.name.strip()
                for r in job.responsibility_ids.sorted(lambda x: (x.sequence, x.id))
                if r.name and r.name.strip()
            ],
            'requirements': _split_lines(job.requirements),
            'preferredSkills': [s.name for s in job.preferred_skill_ids if s.name],
            'benefits': [
                b.name.strip()
                for b in job.benefit_ids.sorted(lambda x: (x.sequence, x.id))
                if b.name and b.name.strip()
            ],
            'screeningPrompt': job.screening_prompt or None,
            'candidateCount': getattr(job, 'application_count', None),
            'approvalRequestedAt': _iso_utc(job.approval_requested_at),
            'approvalDecidedAt': _iso_utc(job.approval_decided_at),
            'approvalEmailSentAt': _iso_utc(job.approval_email_sent_at),
            'requestedBy': job.external_requested_by or (job.requested_by_id.login if job.requested_by_id else None),
            'approvedBy': job.approved_by_id.name if job.approved_by_id else None,
            'approvalRecipientEmail': job.approval_recipient_email or None,
            'reviewedByEmail': job.reviewed_by_email or None,
            'rejectionReason': job.rejection_reason or None,
            'approvers': [
                {
                    'id': a.id,
                    'email': a.email or '',
                    'role': a.role or '',
                    'sequence': a.sequence,
                }
                for a in job.approver_ids.sorted(lambda x: (x.sequence, x.id))
            ],
        })

    return data


class EtharaJobController(http.Controller):

    @http.route(
        '/api/v1/job/list',
        methods=['GET', 'POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_request({})
    def list_jobs(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}

            domain = []

            if 'is_active' in jdata:
                domain.append(('active', '=', _parse_bool(jdata.get('is_active'))))
            else:
                domain.append(('active', '=', True))

            if jdata.get('approval_status'):
                domain.append(('approval_status', '=', jdata.get('approval_status')))
            else:
                domain.append(('approval_status', '=', 'posted'))

            search = (jdata.get('search') or '').strip()
            if search:
                domain += [
                    '|', '|',
                    ('name', 'ilike', search),
                    ('summary', 'ilike', search),
                    ('description', 'ilike', search),
                ]

            department_id = _parse_int(jdata.get('department_id'))
            if department_id:
                domain.append(('department_id', '=', department_id))

            if jdata.get('employment_type'):
                domain.append(('employment_type', '=', jdata.get('employment_type')))
            if jdata.get('work_mode'):
                domain.append(('work_mode', '=', jdata.get('work_mode')))
            if jdata.get('experience_level'):
                domain.append(('experience_level', '=', jdata.get('experience_level')))
            if jdata.get('urgency_level'):
                domain.append(('urgency_level', '=', str(jdata.get('urgency_level'))))
            if jdata.get('job_location'):
                domain.append(('job_location', 'ilike', jdata.get('job_location')))

            if 'is_featured' in jdata:
                domain.append(('is_featured', '=', _parse_bool(jdata.get('is_featured'))))

            min_exp = jdata.get('min_experience_years')
            if min_exp not in (None, ''):
                try:
                    domain.append(('experience_years', '>=', float(min_exp)))
                except (TypeError, ValueError):
                    pass
            max_exp = jdata.get('max_experience_years')
            if max_exp not in (None, ''):
                try:
                    domain.append(('experience_years', '<=', float(max_exp)))
                except (TypeError, ValueError):
                    pass

            posted_from = _parse_datetime(jdata.get('posted_from'))
            if posted_from:
                domain.append(('posted_at', '>=', posted_from))
            posted_to = _parse_datetime(jdata.get('posted_to'))
            if posted_to:
                domain.append(('posted_at', '<=', posted_to))

            sort_by = (jdata.get('sort_by') or 'posted_at').strip()
            if sort_by not in ALLOWED_SORT_FIELDS:
                sort_by = 'posted_at'
            order_dir = (jdata.get('order') or 'desc').strip().lower()
            if order_dir not in ('asc', 'desc'):
                order_dir = 'desc'
            order = '%s %s, id desc' % (sort_by, order_dir)

            page = max(_parse_int(jdata.get('page'), 1) or 1, 1)
            page_size = _parse_int(jdata.get('page_size'), 20) or 20
            page_size = max(1, min(page_size, 100))
            offset = (page - 1) * page_size

            Job = request.env['hr.job'].sudo()
            total = Job.search_count(domain)
            jobs = Job.search(domain, offset=offset, limit=page_size, order=order)

            records = [_serialize_job(job, detail=False) for job in jobs]
            total_pages = (total + page_size - 1) // page_size if page_size else 0

            return return_Response(
                message="Success",
                status=200,
                data={
                    'records': records,
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
        except Exception as e:
            _logger.error('List jobs error: %s', str(e))
            return return_Response(
                message="Something went wrong. Please try again.",
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/job/apply_for_job_position',
        methods=['POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_request({
        'applicant_id': {'required': True, 'type': 'int'},
        'job_id': {'required': True, 'type': 'int'},
    })
    def apply_for_job_position(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            applicant_id = _parse_int(jdata.get('applicant_id'))
            job_id = _parse_int(jdata.get('job_id'))

            if not applicant_id or not job_id:
                return return_Response(
                    message="Both 'applicant_id' and 'job_id' are required.",
                    status=400,
                )

            applicant = request.env['hr.applicant'].sudo().browse(applicant_id).exists()
            if not applicant:
                return return_Response(
                    message="Applicant not found.",
                    status=404,
                )

            job = request.env['hr.job'].sudo().browse(job_id).exists()
            if not job:
                return return_Response(
                    message="Job posting not found.",
                    status=404,
                )

            if applicant.job_id.id == job.id:
                return return_Response(
                    message="Applicant is already applied to this job.",
                    status=400,
                )

            if applicant.job_id:
                return return_Response(
                    message="Applicant is already applied to the job.",
                    status=400,
                )

            vals = {'job_id': job.id}
            if job.department_id:
                vals['department_id'] = job.department_id.id
            applicant.write(vals)

            history_records = applicant.job_history_ids.sorted(
                lambda h: (h.changed_at or datetime.min, h.id),
                reverse=True,
            )

            return return_Response(
                message="Applicant applied to job successfully.",
                status=200,
                data={
                    'record': {
                        'applicant_id': applicant.id,
                        'job_id': applicant.job_id.id if applicant.job_id else None,
                        'job_title': applicant.job_id.name if applicant.job_id else None,
                        'department_id': applicant.department_id.id if applicant.department_id else None,
                        'department_name': applicant.department_id.name if applicant.department_id else None,
                        'history': [
                            {
                                'id': h.id,
                                'previous_job_id': h.previous_job_id.id if h.previous_job_id else None,
                                'previous_job_name': h.previous_job_id.name if h.previous_job_id else None,
                                'new_job_id': h.new_job_id.id if h.new_job_id else None,
                                'new_job_name': h.new_job_id.name if h.new_job_id else None,
                                'changed_by_id': h.changed_by_id.id if h.changed_by_id else None,
                                'changed_by': h.changed_by_id.name if h.changed_by_id else None,
                                'changed_at': _iso_utc(h.changed_at),
                            }
                            for h in history_records
                        ],
                    }
                },
            )
        except Exception as e:
            _logger.error('Apply for job position error: %s', str(e))
            return return_Response(
                message="Something went wrong. Please try again.",
                status=400,
                errors=[str(e)],
            )

    @http.route(
        '/api/v1/job/detail',
        methods=['GET', 'POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_request({})
    def get_job_detail(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}

            job_id = _parse_int(jdata.get('id'))
            slug = (jdata.get('slug') or '').strip()

            if not job_id and not slug:
                return return_Response(
                    message="Please provide either 'id' or 'slug'.",
                    status=400,
                )

            Job = request.env['hr.job'].sudo()
            job = Job.browse()
            if job_id:
                job = Job.browse(job_id).exists()
            elif slug:
                job = Job.search([('slug', '=', slug)], limit=1)

            if not job:
                return return_Response(
                    message="Job posting not found.",
                    status=404,
                )

            return return_Response(
                message="Success",
                status=200,
                data={'record': _serialize_job(job, detail=True)},
            )
        except Exception as e:
            _logger.error('Get job detail error: %s', str(e))
            return return_Response(
                message="Something went wrong. Please try again.",
                status=400,
                errors=[str(e)],
            )
