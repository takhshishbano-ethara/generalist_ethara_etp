import base64
import logging

from odoo import http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    generate_s3_link,
)
from odoo.addons.api_auth_gateway.controllers.main import validate_request

_logger = logging.getLogger(__name__)


ALLOWED_DOCUMENT_TYPES = {
    'passport_size_photo', 'marksheet_10th', 'marksheet_12th', 'graduation',
    'post_graduation', 'certifications', 'experience_letter_1',
    'experience_letter_2', 'relieving_letter', 'payslips', 'pan_doc',
    'aadhaar_doc', 'permanent_address_proof', 'current_address_proof',
    'cancelled_cheque',
}

ALLOWED_MIME_TYPES = {
    'application/pdf', 'image/jpeg', 'image/png', 'image/webp',
}

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024

TRUTHY = {'1', 'true', 'yes', 'y', 'on'}


def _parse_bool(v):
    return isinstance(v, str) and v.strip().lower() in TRUTHY


def _serialize_document(doc):
    return {
        'id': doc.id,
        'applicant_id': doc.applicant_id.id,
        'document_type': doc.document_type,
        's3_url': doc.s3_url or '',
        'file_name': doc.file_name or '',
        'mime_type': doc.mime_type or '',
        'file_size': doc.file_size or 0,
        'verification_status': doc.verification_status or 'pending',
        'verification_confidence': float(doc.verification_confidence or 0.0),
        'ocr_matched_keywords': doc.ocr_matched_keywords or '',
        'verification_error': doc.verification_error or '',
        'uploaded_at': doc.uploaded_at.isoformat() if doc.uploaded_at else '',
        'verified_at': doc.verified_at.isoformat() if doc.verified_at else '',
    }


class ApplicantSelectionFormDocumentController(http.Controller):

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/document',
        type='http', auth='none',
        methods=['POST'], csrf=False, cors='*',
    )
    @validate_request({})
    def upsert_document(self, applicant_id, **kwargs):
        try:
            document_type = (kwargs.get('document_type') or '').strip()
            wait = _parse_bool(kwargs.get('wait'))

            if document_type not in ALLOWED_DOCUMENT_TYPES:
                return return_Response(
                    'Invalid document_type', 400,
                    errors=[
                        'document_type must be one of: '
                        + ', '.join(sorted(ALLOWED_DOCUMENT_TYPES))
                    ],
                )

            uploaded = request.httprequest.files.get('file')
            if not uploaded or not uploaded.filename:
                return return_Response(
                    'File is required', 400,
                    errors=['multipart field "file" is required'],
                )

            mime = (uploaded.mimetype or '').lower()
            if mime not in ALLOWED_MIME_TYPES:
                return return_Response(
                    'Unsupported file type', 400,
                    errors=[
                        f'MIME type "{mime}" not allowed. Allowed: '
                        + ', '.join(sorted(ALLOWED_MIME_TYPES))
                    ],
                )

            raw = uploaded.read()
            if not raw:
                return return_Response(
                    'Empty file', 400, errors=['file has no content'],
                )
            if len(raw) > MAX_FILE_SIZE_BYTES:
                return return_Response(
                    'File must be under 10 MB', 400,
                    errors=[
                        f'file size {len(raw)} bytes exceeds limit of '
                        f'{MAX_FILE_SIZE_BYTES} bytes'
                    ],
                )

            applicant = request.env['hr.applicant'].sudo().browse(applicant_id)
            if not applicant.exists():
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )

            b64 = base64.b64encode(raw)
            s3_url = generate_s3_link(
                b64, prefix='applicant_docs', filename=uploaded.filename,
            )
            if not s3_url:
                return return_Response(
                    'S3 upload failed', 502,
                    errors=['generate_s3_link returned empty URL'],
                )

            Doc = request.env['hr.applicant.document'].sudo()
            if wait:
                Doc = Doc.with_context(skip_verify_schedule=True)

            existing = Doc.search([
                ('applicant_id', '=', applicant.id),
                ('document_type', '=', document_type),
            ], limit=1)

            vals = {
                's3_url': s3_url,
                'file_name': uploaded.filename,
                'mime_type': mime,
                'file_size': len(raw),
            }

            if existing:
                vals.update({'verification_status': 'pending'})
                existing.write(vals)
                record = existing
            else:
                record = Doc.create({
                    'applicant_id': applicant.id,
                    'document_type': document_type,
                    **vals,
                })

            return return_Response(
                'Document uploaded', 200,
                data={'record': _serialize_document(record)},
            )
        except Exception as exc:
            _logger.exception('upsert_document failed')
            return return_Response('Internal error', 500, errors=[str(exc)])
