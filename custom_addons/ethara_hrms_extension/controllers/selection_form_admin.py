import csv
import io
import logging

from odoo import fields as odoo_fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.main import validate_request
from odoo.addons.api_auth_gateway.controllers.utility import return_Response

_logger = logging.getLogger(__name__)


UI_STATUSES = ('pending_send', 'sent', 'submitted', 'validated', 'rejected')

DEFAULT_ELIGIBLE_PIPELINE_STATUSES = (
    'submission', 'contract', 'compliance', 'email_id', 'onboarded',
)


def _derive_ui_status(applicant):
    status = applicant.selection_form_status or 'draft'
    if status == 'approved':
        return 'validated'
    if status == 'rejected':
        return 'rejected'
    if status in ('submitted', 'under_review'):
        return 'submitted'
    return 'sent' if applicant.selection_form_sent_at else 'pending_send'


def _iso(value):
    return value.isoformat() if value else ''


def _serialize_row(applicant):
    ui_status = _derive_ui_status(applicant)
    return {
        'id': applicant.id,
        'candidate_code': applicant.candidate_code or '',
        'name': applicant.partner_name or applicant.name or '',
        'email': applicant.email_from or '',
        'phone': applicant.partner_phone or '',
        'position': applicant.job_id.name if applicant.job_id else '',
        'job_id': applicant.job_id.id if applicant.job_id else False,
        'department': applicant.department_id.name if applicant.department_id else '',
        'stage': applicant.stage_id.name if applicant.stage_id else '',
        'pipeline_status': applicant.pipeline_status or '',
        'selection_form_status': applicant.selection_form_status or 'draft',
        'ui_status': ui_status,
        'sent_at': _iso(applicant.selection_form_sent_at),
        'sent_by': (
            applicant.selection_form_sent_by_id.name
            if applicant.selection_form_sent_by_id else ''
        ),
        'submitted_at': _iso(applicant.selection_form_submitted_at),
        'reviewed_at': _iso(applicant.selection_form_reviewed_at),
        'reviewed_by': (
            applicant.selection_form_reviewed_by_id.name
            if applicant.selection_form_reviewed_by_id else ''
        ),
        'rejection_reason': applicant.selection_form_rejection_reason or '',
    }


def _compute_stats(base_domain):
    Applicant = request.env['hr.applicant'].sudo()
    total_domain = list(base_domain)
    all_records = Applicant.search(total_domain)

    counts = {k: 0 for k in UI_STATUSES}
    for rec in all_records:
        counts[_derive_ui_status(rec)] += 1
    counts['total'] = len(all_records)
    return counts


def _base_domain(kwargs):
    if _parse_bool(kwargs.get('include_all')):
        return []
    return [
        ('pipeline_status', 'in', list(DEFAULT_ELIGIBLE_PIPELINE_STATUSES)),
    ]


def _parse_bool(v):
    return isinstance(v, str) and v.strip().lower() in (
        '1', 'true', 'yes', 'y', 'on',
    )


def _build_search_domain(kwargs):
    domain = _base_domain(kwargs)
    search = (kwargs.get('search') or '').strip()
    if search:
        domain += [
            '|', '|', '|',
            ('partner_name', 'ilike', search),
            ('name', 'ilike', search),
            ('email_from', 'ilike', search),
            ('candidate_code', 'ilike', search),
        ]
    job_id = kwargs.get('job_id')
    if job_id:
        try:
            domain.append(('job_id', '=', int(job_id)))
        except (TypeError, ValueError):
            pass
    return domain


def _apply_ui_status_filter(records, ui_status):
    if not ui_status or ui_status not in UI_STATUSES:
        return records
    return records.filtered(lambda r: _derive_ui_status(r) == ui_status)


class SelectionFormAdminController(http.Controller):

    @http.route(
        '/api/v1/hrms/selection-forms',
        type='http', auth='none',
        methods=['GET'], csrf=False, cors='*',
    )
    @validate_request({})
    def list_selection_forms(self, **kwargs):
        try:
            domain = _build_search_domain(kwargs)
            Applicant = request.env['hr.applicant'].sudo()

            ui_status = (kwargs.get('ui_status') or '').strip().lower() or None

            sort_by = (kwargs.get('sort_by') or 'write_date').strip()
            if sort_by not in (
                'write_date', 'create_date', 'name', 'partner_name',
                'selection_form_submitted_at', 'selection_form_sent_at',
            ):
                sort_by = 'write_date'
            order_dir = (kwargs.get('order') or 'desc').strip().lower()
            if order_dir not in ('asc', 'desc'):
                order_dir = 'desc'
            order = f'{sort_by} {order_dir}, id desc'

            try:
                page = max(1, int(kwargs.get('page') or 1))
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = min(100, max(1, int(kwargs.get('page_size') or 20)))
            except (TypeError, ValueError):
                page_size = 20

            if ui_status:
                all_recs = Applicant.search(domain, order=order)
                filtered = _apply_ui_status_filter(all_recs, ui_status)
                total = len(filtered)
                start = (page - 1) * page_size
                page_recs = filtered[start:start + page_size]
            else:
                total = Applicant.search_count(domain)
                page_recs = Applicant.search(
                    domain, order=order,
                    limit=page_size, offset=(page - 1) * page_size,
                )

            stats = _compute_stats(_base_domain(kwargs))

            return return_Response(
                'ok', 200,
                data={
                    'records': [_serialize_row(r) for r in page_recs],
                    'pagination': {
                        'page': page,
                        'page_size': page_size,
                        'total': total,
                        'has_more': (page * page_size) < total,
                    },
                    'stats': stats,
                },
            )
        except Exception as exc:
            _logger.exception('list_selection_forms failed')
            return return_Response(
                'Internal error', 500, errors=[str(exc)],
            )

    @http.route(
        '/api/v1/hrms/selection-forms/stats',
        type='http', auth='none',
        methods=['GET'], csrf=False, cors='*',
    )
    @validate_request({})
    def selection_forms_stats(self, **kwargs):
        try:
            stats = _compute_stats(_base_domain(kwargs))
            return return_Response('ok', 200, data={'stats': stats})
        except Exception as exc:
            _logger.exception('selection_forms_stats failed')
            return return_Response(
                'Internal error', 500, errors=[str(exc)],
            )

    @http.route(
        '/api/v1/hrms/selection-forms/export',
        type='http', auth='none',
        methods=['GET'], csrf=False, cors='*',
    )
    @validate_request({})
    def export_selection_forms(self, **kwargs):
        try:
            domain = _build_search_domain(kwargs)
            ui_status = (kwargs.get('ui_status') or '').strip().lower() or None
            Applicant = request.env['hr.applicant'].sudo()
            records = Applicant.search(domain, order='write_date desc, id desc')
            if ui_status and ui_status in UI_STATUSES:
                records = _apply_ui_status_filter(records, ui_status)

            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow([
                'Candidate Code', 'Name', 'Email', 'Phone', 'Position',
                'Department', 'Stage', 'UI Status',
                'Selection Form Status', 'Sent At', 'Sent By',
                'Submitted At', 'Reviewed At', 'Reviewed By',
                'Rejection Reason',
            ])
            for r in records:
                writer.writerow([
                    r.candidate_code or '',
                    r.partner_name or r.name or '',
                    r.email_from or '',
                    r.partner_phone or '',
                    r.job_id.name if r.job_id else '',
                    r.department_id.name if r.department_id else '',
                    r.stage_id.name if r.stage_id else '',
                    _derive_ui_status(r),
                    r.selection_form_status or 'draft',
                    _iso(r.selection_form_sent_at),
                    r.selection_form_sent_by_id.name if r.selection_form_sent_by_id else '',
                    _iso(r.selection_form_submitted_at),
                    _iso(r.selection_form_reviewed_at),
                    r.selection_form_reviewed_by_id.name if r.selection_form_reviewed_by_id else '',
                    (r.selection_form_rejection_reason or '').replace('\n', ' ').strip(),
                ])
            payload = buffer.getvalue().encode('utf-8')
            filename = 'selection_forms_%s.csv' % odoo_fields.Datetime.now().strftime(
                '%Y%m%d_%H%M%S',
            )
            return request.make_response(
                payload,
                headers=[
                    ('Content-Type', 'text/csv; charset=utf-8'),
                    ('Content-Disposition', f'attachment; filename="{filename}"'),
                    ('Content-Length', str(len(payload))),
                ],
            )
        except Exception as exc:
            _logger.exception('export_selection_forms failed')
            return return_Response(
                'Internal error', 500, errors=[str(exc)],
            )

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/send',
        type='http', auth='none',
        methods=['POST'], csrf=False, cors='*',
    )
    @validate_request({})
    def send_selection_form(self, applicant_id, **kwargs):
        try:
            applicant = _get_applicant_or_404(applicant_id)
            if not applicant:
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )
            if applicant.selection_form_status not in ('draft',):
                return return_Response(
                    'Form is not in a sendable state', 400,
                    errors=[
                        'Only draft forms can be sent. Current status: '
                        f'{applicant.selection_form_status}',
                    ],
                )

            now = odoo_fields.Datetime.now()
            resend = _parse_bool(kwargs.get('resend'))
            if applicant.selection_form_sent_at and not resend:
                return return_Response(
                    'Form already sent', 400,
                    errors=[
                        'selection_form_sent_at is already set. '
                        'Pass resend=true to force a new invite.',
                    ],
                )

            applicant.write({
                'selection_form_sent_at': now,
                'selection_form_sent_by_id': request.env.uid or applicant.selection_form_sent_by_id.id or False,
            })

            _send_selection_form_email(applicant)

            return return_Response(
                'Selection form sent', 200,
                data={'record': _serialize_row(applicant)},
            )
        except (UserError, ValidationError) as exc:
            return return_Response('Validation failed', 400, errors=[str(exc)])
        except Exception as exc:
            _logger.exception('send_selection_form failed')
            return return_Response('Internal error', 500, errors=[str(exc)])

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/reopen',
        type='http', auth='none',
        methods=['POST'], csrf=False, cors='*',
    )
    @validate_request({})
    def reopen_selection_form(self, applicant_id, **kwargs):
        try:
            applicant = _get_applicant_or_404(applicant_id)
            if not applicant:
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )
            if applicant.selection_form_status not in (
                'submitted', 'under_review', 'approved', 'rejected',
            ):
                return return_Response(
                    'Form is not in a reopenable state', 400,
                    errors=[
                        'Only submitted / under_review / approved / rejected '
                        'forms can be reopened. Current status: '
                        f'{applicant.selection_form_status}',
                    ],
                )
            applicant.write({
                'selection_form_status': 'draft',
                'selection_form_submitted_at': False,
                'selection_form_reviewed_at': False,
                'selection_form_reviewed_by_id': False,
                'selection_form_rejection_reason': False,
            })
            return return_Response(
                'Selection form reopened', 200,
                data={'record': _serialize_row(applicant)},
            )
        except (UserError, ValidationError) as exc:
            return return_Response('Validation failed', 400, errors=[str(exc)])
        except Exception as exc:
            _logger.exception('reopen_selection_form failed')
            return return_Response('Internal error', 500, errors=[str(exc)])

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/documents/verify',
        type='http', auth='none',
        methods=['POST'], csrf=False, cors='*',
    )
    @validate_request({})
    def verify_all_documents(self, applicant_id, **kwargs):
        try:
            applicant = _get_applicant_or_404(applicant_id)
            if not applicant:
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )
            docs = applicant.selection_form_document_ids
            if not docs:
                return return_Response(
                    'No documents to verify', 200,
                    data={'scheduled_ids': []},
                )
            docs.sudo().write({'verification_status': 'pending'})
            docs.sudo()._schedule_verification()
            return return_Response(
                'Verification scheduled', 202,
                data={
                    'scheduled_ids': docs.ids,
                    'count': len(docs),
                },
            )
        except (UserError, ValidationError) as exc:
            return return_Response('Validation failed', 400, errors=[str(exc)])
        except Exception as exc:
            _logger.exception('verify_all_documents failed')
            return return_Response('Internal error', 500, errors=[str(exc)])

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/document/<int:doc_id>/verify',
        type='http', auth='none',
        methods=['POST'], csrf=False, cors='*',
    )
    @validate_request({})
    def verify_single_document(self, applicant_id, doc_id, **kwargs):
        try:
            applicant = _get_applicant_or_404(applicant_id)
            if not applicant:
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )
            doc = request.env['hr.applicant.document'].sudo().browse(doc_id)
            if not doc.exists() or doc.applicant_id.id != applicant.id:
                return return_Response(
                    'Document not found', 404,
                    errors=[
                        f'no hr.applicant.document id={doc_id} '
                        f'belonging to applicant id={applicant_id}',
                    ],
                )
            doc._run_verification_sync()
            doc.invalidate_recordset()
            return return_Response(
                'Document verified', 200,
                data={
                    'record': {
                        'id': doc.id,
                        'document_type': doc.document_type,
                        'verification_status': doc.verification_status or 'pending',
                        'verification_confidence': float(doc.verification_confidence or 0.0),
                        'ocr_matched_keywords': doc.ocr_matched_keywords or '',
                        'verification_error': doc.verification_error or '',
                        'verified_at': _iso(doc.verified_at),
                    },
                },
            )
        except (UserError, ValidationError) as exc:
            return return_Response('Validation failed', 400, errors=[str(exc)])
        except Exception as exc:
            _logger.exception('verify_single_document failed')
            return return_Response('Internal error', 500, errors=[str(exc)])


def _get_applicant_or_404(applicant_id):
    applicant = request.env['hr.applicant'].sudo().browse(applicant_id)
    return applicant if applicant.exists() else None


def _send_selection_form_email(applicant):
    recipient = (applicant.email_from or '').strip()
    if not recipient:
        _logger.warning(
            'send_selection_form: applicant %s has no email_from — skipping mail.',
            applicant.id,
        )
        return

    ICP = request.env['ir.config_parameter'].sudo()
    base_url = ICP.get_param(
        'ethara_hrms.selection_form_base_url',
        default=ICP.get_param('web.base.url', 'http://localhost:8069'),
    )
    form_link = '%s/selection-form/%s' % (
        base_url.rstrip('/'), applicant.candidate_code or applicant.id,
    )
    email_from = ICP.get_param(
        'mail.catchall.email', 'no-reply@kuberha.ai',
    )
    subject = 'Please complete your selection form – %s' % (
        applicant.job_id.name if applicant.job_id else 'Ethara',
    )
    body_html = (
        '<div style="font-family:Arial,sans-serif;max-width:600px;'
        'margin:0 auto;padding:20px;">'
        '<h2 style="color:#333;">Hi %s,</h2>'
        '<p>Thanks for progressing to the next stage%s. '
        'Please complete your selection form using the secure link below:</p>'
        '<p style="text-align:center;margin:30px 0;">'
        '<a href="%s" style="background-color:#7c3aed;color:#fff;'
        'padding:12px 30px;text-decoration:none;border-radius:6px;'
        'font-size:16px;">Open Selection Form</a></p>'
        '<p style="color:#666;font-size:13px;">Reference code: '
        '<strong>%s</strong></p>'
        '<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">'
        '<p style="color:#999;font-size:11px;">Ethara Team</p>'
        '</div>'
    ) % (
        applicant.partner_name or 'Candidate',
        (' for %s' % applicant.job_id.name) if applicant.job_id else '',
        form_link,
        applicant.candidate_code or applicant.id,
    )
    try:
        mail = request.env['mail.mail'].sudo().create({
            'subject': subject,
            'email_from': email_from,
            'email_to': recipient,
            'body_html': body_html,
            'auto_delete': False,
        })
        mail.send(raise_exception=False)
        _logger.info(
            'send_selection_form: queued mail_id=%s to %s for applicant %s',
            mail.id, recipient, applicant.id,
        )
    except Exception:
        _logger.exception(
            'send_selection_form: failed to send invite for applicant %s',
            applicant.id,
        )
