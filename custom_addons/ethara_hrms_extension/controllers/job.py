import logging
import re
from datetime import datetime

from odoo import http, fields
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    validate_request,
    validate_token,
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


def _csv_to_list(text):
    """Split a stored comma-separated string into a clean list of keywords."""
    if not text:
        return []
    return [p.strip() for p in text.split(',') if p.strip()]


# ---------------------------------------------------------------------------
# Write-side helpers (create / update)
# ---------------------------------------------------------------------------

# Optional selection fields that accept either the stored technical key or the
# human label (case-insensitive). urgency_level accepts its numeric key too.
_SELECTION_FIELDS = ('employment_type', 'work_mode', 'experience_level',
                     'urgency_level', 'approval_status')


def _pick(jdata, *keys):
    """Return (present, value) for the first key found in the payload.

    Accepting several aliases keeps the API forgiving to both snake_case and
    the camelCase keys returned by the GET/list serializers.
    """
    for key in keys:
        if key in jdata:
            return True, jdata[key]
    return False, None


def _coerce_selection(field_name, value):
    """Map an incoming value to a valid selection key.

    Returns (key, error). Matches the technical key first, then falls back to a
    case-insensitive label match so callers may send either representation.
    """
    field = request.env['hr.job']._fields.get(field_name)
    options = field._description_selection(request.env) if field else []
    value_str = str(value).strip()
    for key, _label in options:
        if value_str == str(key):
            return key, None
    for key, label in options:
        if value_str.lower() == str(label).lower():
            return key, None
    allowed = ', '.join(str(k) for k, _ in options)
    return None, "Invalid value '%s' for '%s'. Allowed: %s" % (
        value, field_name, allowed)


def _split_multiline(value):
    """Normalize a 'one per line' input (list or newline string) to a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [p.strip() for p in str(value).splitlines()]
    return [i for i in items if i]


def _split_csv_input(value):
    """Normalize a 'comma separated' input (list or string) to a deduped list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = [str(v).strip() for v in value]
    else:
        raw = [p.strip() for p in str(value).split(',')]
    seen = set()
    out = []
    for item in raw:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def _slugify(text):
    text = (text or '').strip().lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = re.sub(r'-+', '-', text).strip('-')
    return text or 'job'


def _unique_slug(base_slug, exclude_id=None):
    """Return base_slug (or base_slug-2, -3, ...) not used by another job."""
    Job = request.env['hr.job'].sudo()
    slug = base_slug
    counter = 1
    while True:
        domain = [('slug', '=', slug)]
        if exclude_id:
            domain.append(('id', '!=', exclude_id))
        if not Job.with_context(active_test=False).search_count(domain):
            return slug
        counter += 1
        slug = '%s-%s' % (base_slug, counter)


def _responsibility_commands(value):
    """Build O2M commands that replace responsibilities with the given lines."""
    commands = [fields.Command.clear()]
    sequence = 10
    for name in _split_multiline(value):
        commands.append(fields.Command.create({'name': name, 'sequence': sequence}))
        sequence += 10
    return commands


def _approver_commands(value):
    """Build O2M commands that replace approvers with the given list.

    Each item may be a dict {'email', 'role'} or a bare email string.
    """
    commands = [fields.Command.clear()]
    if not isinstance(value, (list, tuple)):
        return commands
    sequence = 10
    for item in value:
        if isinstance(item, dict):
            email = (item.get('email') or '').strip()
            role = (item.get('role') or 'recipient').strip()
        else:
            email = str(item).strip()
            role = 'recipient'
        if not email:
            continue
        if role not in ('recipient', 'reviewer'):
            role = 'recipient'
        commands.append(fields.Command.create({
            'email': email, 'role': role, 'sequence': sequence,
        }))
        sequence += 10
    return commands


def _build_scalar_vals(jdata, job=None):
    """Translate an incoming payload into hr.job write values.

    Only keys present in ``jdata`` are set, so the same builder serves both
    create (``job`` is None) and partial update. Returns (vals, errors).
    """
    is_create = job is None
    vals = {}
    errors = []

    present, title = _pick(jdata, 'title', 'name', 'job_title')
    if present:
        title = (title or '').strip() if isinstance(title, str) else title
        if not title:
            errors.append('Job title cannot be empty.')
        else:
            vals['name'] = title
    elif is_create:
        errors.append('Job title is required.')

    present, dept = _pick(jdata, 'department_id', 'departmentId')
    if present:
        dept_id = _parse_int(dept)
        if not dept_id:
            errors.append('A valid department_id is required.')
        else:
            department = request.env['hr.department'].sudo().browse(dept_id).exists()
            if not department:
                errors.append("Department with id %s was not found." % dept_id)
            else:
                vals['department_id'] = dept_id
    elif is_create:
        errors.append('Department is required.')

    # Free-text fields -------------------------------------------------------
    present, v = _pick(jdata, 'summary')
    if present:
        vals['summary'] = (v or '').strip() or False if isinstance(v, str) else (v or False)

    present, v = _pick(jdata, 'description')
    if present:
        vals['description'] = (v.strip() or False) if isinstance(v, str) else (v or False)

    present, v = _pick(jdata, 'location', 'job_location', 'jobLocation')
    if present:
        vals['job_location'] = (v or '').strip() or False if isinstance(v, str) else (v or False)

    present, v = _pick(jdata, 'salary_bracket', 'salaryBracket')
    if present:
        vals['salary_bracket'] = (v or '').strip() or False if isinstance(v, str) else (v or False)

    present, v = _pick(jdata, 'screening_prompt', 'screeningPrompt',
                       'llm_screening_prompt', 'llmScreeningPrompt')
    if present:
        vals['screening_prompt'] = (v or '').strip() or False if isinstance(v, str) else (v or False)

    present, v = _pick(jdata, 'deactivation_reason', 'deactivationReason',
                       'deactivate_reason', 'deactivateReason')
    if present:
        vals['deactivation_reason'] = (v or '').strip() or False if isinstance(v, str) else (v or False)

    # Selection fields -------------------------------------------------------
    for field_name, keys in (
        ('employment_type', ('employment_type', 'employmentType')),
        ('work_mode', ('work_mode', 'workMode')),
        ('experience_level', ('experience_level', 'experienceLevel')),
        ('urgency_level', ('urgency_level', 'urgencyLevel')),
    ):
        present, v = _pick(jdata, *keys)
        if not present:
            continue
        if v in (None, ''):
            vals[field_name] = False
            continue
        key, err = _coerce_selection(field_name, v)
        if err:
            errors.append(err)
        else:
            vals[field_name] = key

    # Experience years (float) ----------------------------------------------
    present, v = _pick(jdata, 'experience_years', 'experienceYears', 'experience')
    if present:
        if v in (None, ''):
            vals['experience_years'] = 0.0
        else:
            try:
                vals['experience_years'] = float(v)
            except (TypeError, ValueError):
                errors.append("'experience_years' must be a number.")

    # Openings (non-negative integer) ---------------------------------------
    present, v = _pick(jdata, 'openings', 'no_of_recruitment', 'noOfRecruitment')
    if present:
        openings = _parse_int(v)
        if openings is None or openings < 0:
            errors.append("'openings' must be a non-negative integer.")
        else:
            vals['no_of_recruitment'] = openings

    # Booleans ---------------------------------------------------------------
    present, v = _pick(jdata, 'is_featured', 'featured', 'isFeatured')
    if present:
        vals['is_featured'] = _parse_bool(v)

    present, v = _pick(jdata, 'is_active', 'active', 'isActive')
    if present:
        vals['active'] = _parse_bool(v)

    # Requirements (one per line -> Text) -----------------------------------
    present, v = _pick(jdata, 'requirements', 'required_skills', 'requiredSkills',
                       'required_skill_set', 'requiredSkillSet')
    if present:
        items = _split_multiline(v)
        vals['requirements'] = '\n'.join(items) if items else False

    # Additional skill keywords (comma separated -> Text) -------------------
    present, v = _pick(jdata, 'skill_keywords', 'skillKeywords',
                       'additional_skill_keywords', 'additionalSkillKeywords',
                       'keywords')
    if present:
        items = _split_csv_input(v)
        vals['skill_keywords'] = ', '.join(items) if items else False

    # Slug -------------------------------------------------------------------
    present, v = _pick(jdata, 'slug')
    if present:
        base = _slugify(v) if v else ''
        vals['slug'] = _unique_slug(base, exclude_id=job.id if job else None) if base else False

    return vals, errors


def _apply_approval(vals, jdata, job=None):
    """Apply approval_status transitions and related fields/timestamps.

    Returns a list of validation errors (usually empty).
    """
    errors = []
    is_create = job is None
    now = fields.Datetime.now()

    present, raw_status = _pick(jdata, 'approval_status', 'approvalStatus')
    present_flag, flag = _pick(jdata, 'submit_for_approval', 'submitForApproval')

    new_status = None
    if present and raw_status:
        status_key, err = _coerce_selection('approval_status', raw_status)
        if err:
            errors.append(err)
        else:
            new_status = status_key
    elif present_flag and _parse_bool(flag):
        new_status = 'requested'

    if new_status:
        vals['approval_status'] = new_status
        if new_status == 'requested':
            vals['approval_requested_at'] = now
        elif new_status == 'posted':
            vals['posted_at'] = job.posted_at if (job and job.posted_at) else now
            vals['approval_decided_at'] = now
        elif new_status == 'rejected':
            vals['approval_decided_at'] = now
    elif is_create:
        vals.setdefault('approval_status', 'draft')

    present, v = _pick(jdata, 'external_requested_by', 'requested_by', 'requestedBy')
    if present:
        vals['external_requested_by'] = (v or '').strip() or False if isinstance(v, str) else (v or False)

    present, v = _pick(jdata, 'approval_recipient_email', 'approvalRecipientEmail',
                       'recipient_emails', 'recipientEmails')
    if present:
        if isinstance(v, (list, tuple)):
            v = ', '.join(str(e).strip() for e in v if str(e).strip())
        vals['approval_recipient_email'] = (v or '').strip() or False

    present, v = _pick(jdata, 'reviewed_by_email', 'reviewedByEmail')
    if present:
        vals['reviewed_by_email'] = (v or '').strip() or False if isinstance(v, str) else (v or False)

    present, v = _pick(jdata, 'rejection_reason', 'rejectionReason')
    if present:
        vals['rejection_reason'] = (v or '').strip() or False if isinstance(v, str) else (v or False)

    return errors


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
        'candidateCount': getattr(job, 'application_count', None),
        'approvalRequestedAt': _iso_utc(job.approval_requested_at),
        'approvalDecidedAt': _iso_utc(job.approval_decided_at),
        'createdAt': _iso_utc(job.create_date),
        'updatedAt': _iso_utc(job.write_date),
        'preferredSkills': [s.name for s in job.preferred_skill_ids if s.name],
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
            'skillKeywords': _csv_to_list(job.skill_keywords),
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
            'deactivationReason': job.deactivation_reason or None,
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

            # Active filter. Pass is_active='all' to include archived positions
            # (used by the admin list); the public careers list omits it and
            # only sees active positions.
            include_archived = False
            raw_active = jdata.get('is_active')
            if 'is_active' in jdata and str(raw_active).strip().lower() == 'all':
                include_archived = True
            elif 'is_active' in jdata:
                domain.append(('active', '=', _parse_bool(raw_active)))
            else:
                domain.append(('active', '=', True))

            # Approval status filter. Defaults to 'posted' for the public list;
            # pass approval_status='all' (or a list of statuses) so the admin
            # list can show drafts / pending-approval / posted together.
            raw_status = jdata.get('approval_status')
            if isinstance(raw_status, (list, tuple)):
                statuses = [str(s).strip() for s in raw_status if str(s).strip()]
                if statuses:
                    domain.append(('approval_status', 'in', statuses))
            elif raw_status:
                if str(raw_status).strip().lower() == 'all':
                    pass
                else:
                    domain.append(('approval_status', '=', str(raw_status).strip()))
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
            if include_archived:
                Job = Job.with_context(active_test=False)
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

    @validate_token
    @http.route(
        '/api/v1/job/create',
        methods=['POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_request({})
    def create_job(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}

            vals, errors = _build_scalar_vals(jdata, job=None)
            errors += _apply_approval(vals, jdata, job=None)
            if errors:
                return return_Response(
                    message=errors[0], status=400, errors=errors,
                )

            # Auto-generate a unique slug from the title when none was supplied.
            if not vals.get('slug'):
                vals['slug'] = _unique_slug(_slugify(vals.get('name')))

            # Track the recruiter creating the position (validate_token has set
            # the request user to the authenticated token holder).
            if request.env.uid:
                vals.setdefault('requested_by_id', request.env.uid)

            present, resp = _pick(jdata, 'responsibilities', 'key_responsibilities',
                                  'keyResponsibilities', 'key_job_responsibilities')
            if present:
                vals['responsibility_ids'] = _responsibility_commands(resp)

            present, approvers = _pick(jdata, 'approvers', 'approver_emails', 'approverEmails')
            if present:
                vals['approver_ids'] = _approver_commands(approvers)

            job = request.env['hr.job'].sudo().create(vals)

            # When the JD is submitted for approval, mint a one-time token and
            # email the configured approvers. Never let a mail issue break create.
            if vals.get('approval_status') == 'requested':
                try:
                    job._submit_for_approval()
                except Exception as e:
                    _logger.error('Create job: approval submission failed: %s', str(e))

            return return_Response(
                message="Job position created successfully.",
                status=200,
                data={'record': _serialize_job(job, detail=True)},
            )
        except Exception as e:
            _logger.error('Create job error: %s', str(e))
            return return_Response(
                message="Something went wrong. Please try again.",
                status=400,
                errors=[str(e)],
            )

    @validate_token
    @http.route(
        '/api/v1/job/update',
        methods=['POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_request({})
    def update_job(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}

            _, raw_id = _pick(jdata, 'id', 'job_id', 'jobId')
            job_id = _parse_int(raw_id)
            if not job_id:
                return return_Response(
                    message="'id' is required to update a job position.",
                    status=400,
                    errors=["'id' is required."],
                )

            job = request.env['hr.job'].sudo().browse(job_id).exists()
            if not job:
                return return_Response(
                    message="Job position not found.",
                    status=404,
                    errors=["Job position not found."],
                )

            vals, errors = _build_scalar_vals(jdata, job=job)
            errors += _apply_approval(vals, jdata, job=job)
            if errors:
                return return_Response(
                    message=errors[0], status=400, errors=errors,
                )

            present, resp = _pick(jdata, 'responsibilities', 'key_responsibilities',
                                  'keyResponsibilities', 'key_job_responsibilities')
            if present:
                vals['responsibility_ids'] = _responsibility_commands(resp)

            present, approvers = _pick(jdata, 'approvers', 'approver_emails', 'approverEmails')
            if present:
                vals['approver_ids'] = _approver_commands(approvers)

            if vals:
                # Pass the real actor so a deactivation notice attributes the
                # action to the authenticated recruiter, not the sudo user.
                job.with_context(
                    deactivation_actor=request.env.user.name
                ).write(vals)

            # Re-submitting for approval (e.g. after edits or a rejection) mints a
            # fresh token and re-emails the approvers.
            if vals.get('approval_status') == 'requested':
                try:
                    job._submit_for_approval()
                except Exception as e:
                    _logger.error('Update job: approval submission failed: %s', str(e))

            return return_Response(
                message="Job position updated successfully.",
                status=200,
                data={'record': _serialize_job(job, detail=True)},
            )
        except Exception as e:
            _logger.error('Update job error: %s', str(e))
            return return_Response(
                message="Something went wrong. Please try again.",
                status=400,
                errors=[str(e)],
            )

    @validate_token
    @http.route(
        '/api/v1/job/delete',
        methods=['POST'],
        type='http',
        auth='none',
        csrf=False,
        cors='*',
    )
    @validate_request({})
    def delete_job(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}

            _, raw_id = _pick(jdata, 'id', 'job_id', 'jobId')
            job_id = _parse_int(raw_id)
            if not job_id:
                return return_Response(
                    message="'id' is required to delete a job position.",
                    status=400,
                    errors=["'id' is required."],
                )

            job = request.env['hr.job'].with_context(active_test=False).sudo().browse(job_id).exists()
            if not job:
                return return_Response(
                    message="Job position not found.",
                    status=404,
                    errors=["Job position not found."],
                )

            try:
                job.unlink()
            except Exception:
                # A job with linked applications cannot be hard-deleted; archive
                # it instead so the caller still gets a clean, actionable result.
                job.with_context(
                    deactivation_actor=request.env.user.name
                ).write({
                    'active': False,
                    'deactivation_reason': 'Deactivated automatically: the position '
                    'has linked applications and could not be deleted.',
                })
                return return_Response(
                    message="Job position has linked records and was deactivated instead of deleted.",
                    status=200,
                    data={'record': {'id': job_id, 'deleted': False, 'active': False}},
                )

            return return_Response(
                message="Job position deleted successfully.",
                status=200,
                data={'record': {'id': job_id, 'deleted': True}},
            )
        except Exception as e:
            _logger.error('Delete job error: %s', str(e))
            return return_Response(
                message="Something went wrong. Please try again.",
                status=400,
                errors=[str(e)],
            )
