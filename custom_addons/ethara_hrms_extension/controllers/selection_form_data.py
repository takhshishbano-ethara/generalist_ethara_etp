import logging

from odoo import fields as odoo_fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.main import validate_request
from odoo.addons.api_auth_gateway.controllers.utility import return_Response

_logger = logging.getLogger(__name__)


EMPLOYMENT_FIELDS = (
    'partner_name', 'email_from', 'partner_phone',
    'date_of_birth', 'experience_type', 'highest_qualification',
)

PROFESSIONAL_FIELDS = (
    'employer_1_name', 'employer_1_designation',
    'employer_1_start_date', 'employer_1_end_date',
    'employer_2_name', 'employer_2_designation',
    'employer_2_start_date', 'employer_2_end_date',
)

FAMILY_FIELDS = (
    'father_name', 'mother_name', 'gender', 'marital',
    'aadhaar_number', 'pan_number',
    'has_uan_number', 'uan_number', 'languages_known',
    'emergency_contact_name', 'emergency_contact_phone',
    'emergency_contact_relation',
)

BANK_FIELDS = (
    'has_savings_account', 'has_salary_account',
    'bank_name', 'bank_account_holder_name',
    'bank_account_number', 'bank_ifsc_code',
)

ADDRESS_FIELDS = ('current_address', 'permanent_address')

SENSITIVE_FIELDS = {'aadhaar_number', 'pan_number', 'bank_account_number'}

DATE_FIELDS = {
    'date_of_birth',
    'employer_1_start_date', 'employer_1_end_date',
    'employer_2_start_date', 'employer_2_end_date',
}

BOOL_FIELDS = {
    'has_uan_number', 'has_savings_account', 'has_salary_account',
}

REQUIRED_ON_SUBMIT_BASE = (
    'partner_name', 'email_from', 'partner_phone',
    'date_of_birth', 'experience_type', 'highest_qualification',
    'father_name', 'mother_name', 'gender', 'marital',
    'aadhaar_number', 'pan_number', 'has_uan_number',
    'emergency_contact_name', 'emergency_contact_phone',
    'emergency_contact_relation',
    'current_address', 'permanent_address',
)

REQUIRED_DOCUMENTS_BASE = (
    'passport_size_photo', 'marksheet_10th', 'marksheet_12th',
    'graduation', 'pan_doc', 'aadhaar_doc', 'permanent_address_proof',
)

REQUIRED_DOCUMENTS_EXPERIENCED = ('experience_letter_1',)


def _mask_aadhaar(value):
    v = (value or '').strip()
    if len(v) != 12 or not v.isdigit():
        return v
    return 'XXXX-XXXX-' + v[-4:]


def _mask_pan(value):
    v = (value or '').strip().upper()
    if len(v) != 10:
        return v
    return 'XXXXXX' + v[-4:]


def _mask_account(value):
    v = (value or '').strip()
    if len(v) < 4:
        return v
    return 'X' * (len(v) - 4) + v[-4:]


def _to_iso(value):
    if not value:
        return ''
    return value.isoformat()


def _pick(kwargs, whitelist):
    return {k: kwargs[k] for k in whitelist if k in kwargs}


def _body(kwargs):
    return kwargs.get('jdata') or {}


def _coerce(vals):
    out = {}
    for k, v in vals.items():
        if v is None:
            out[k] = False
        elif k in BOOL_FIELDS:
            if isinstance(v, bool):
                out[k] = v
            elif isinstance(v, str):
                out[k] = v.strip().lower() in ('1', 'true', 'yes', 'y', 'on')
            else:
                out[k] = bool(v)
        elif k in DATE_FIELDS and isinstance(v, str) and v.strip():
            out[k] = v.strip()
        elif k == 'pan_number' and isinstance(v, str):
            out[k] = v.strip().upper() or False
        elif k == 'bank_ifsc_code' and isinstance(v, str):
            out[k] = v.strip().upper() or False
        elif isinstance(v, str):
            out[k] = v.strip() or False
        else:
            out[k] = v
    return out


def _serialize_reference(ref):
    return {
        'id': ref.id,
        'sequence': ref.sequence,
        'name': ref.name or '',
        'email': ref.email or '',
        'phone': ref.phone or '',
        'linkedin_url': ref.linkedin_url or '',
    }


def _serialize_document(doc):
    return {
        'id': doc.id,
        'document_type': doc.document_type,
        's3_url': doc.s3_url or '',
        'file_name': doc.file_name or '',
        'mime_type': doc.mime_type or '',
        'file_size': doc.file_size or 0,
        'verification_status': doc.verification_status or 'pending',
        'uploaded_at': _to_iso(doc.uploaded_at),
        'verified_at': _to_iso(doc.verified_at),
    }


def _serialize_applicant(applicant, mask=True):
    aadhaar = _mask_aadhaar(applicant.aadhaar_number) if mask else (
        applicant.aadhaar_number or ''
    )
    pan = _mask_pan(applicant.pan_number) if mask else (
        applicant.pan_number or ''
    )
    account = _mask_account(applicant.bank_account_number) if mask else (
        applicant.bank_account_number or ''
    )
    return {
        'id': applicant.id,
        'selection_form_status': applicant.selection_form_status or 'draft',
        'selection_form_submitted_at': _to_iso(applicant.selection_form_submitted_at),
        'selection_form_reviewed_at': _to_iso(applicant.selection_form_reviewed_at),
        'selection_form_reviewed_by_id': applicant.selection_form_reviewed_by_id.id or False,
        'selection_form_rejection_reason': applicant.selection_form_rejection_reason or '',
        'selection_form_reopen_reason': applicant.selection_form_reopen_reason or '',
        'employment': {
            'partner_name': applicant.partner_name or '',
            'email_from': applicant.email_from or '',
            'partner_phone': applicant.partner_phone or '',
            'date_of_birth': _to_iso(applicant.date_of_birth),
            'experience_type': applicant.experience_type or '',
            'highest_qualification': applicant.highest_qualification or '',
        },
        'professional': {
            'employer_1_name': applicant.employer_1_name or '',
            'employer_1_designation': applicant.employer_1_designation or '',
            'employer_1_start_date': _to_iso(applicant.employer_1_start_date),
            'employer_1_end_date': _to_iso(applicant.employer_1_end_date),
            'employer_2_name': applicant.employer_2_name or '',
            'employer_2_designation': applicant.employer_2_designation or '',
            'employer_2_start_date': _to_iso(applicant.employer_2_start_date),
            'employer_2_end_date': _to_iso(applicant.employer_2_end_date),
        },
        'family': {
            'father_name': applicant.father_name or '',
            'mother_name': applicant.mother_name or '',
            'gender': applicant.gender or '',
            'marital': applicant.marital or '',
            'aadhaar_number': aadhaar,
            'pan_number': pan,
            'has_uan_number': applicant.has_uan_number,
            'uan_number': applicant.uan_number or '',
            'languages_known': applicant.languages_known or '',
            'emergency_contact_name': applicant.emergency_contact_name or '',
            'emergency_contact_phone': applicant.emergency_contact_phone or '',
            'emergency_contact_relation': applicant.emergency_contact_relation or '',
        },
        'references': [_serialize_reference(r) for r in applicant.reference_ids],
        'bank': {
            'has_savings_account': applicant.has_savings_account,
            'has_salary_account': applicant.has_salary_account,
            'bank_name': applicant.bank_name or '',
            'bank_account_holder_name': applicant.bank_account_holder_name or '',
            'bank_account_number': account,
            'bank_ifsc_code': applicant.bank_ifsc_code or '',
        },
        'address': {
            'current_address': applicant.current_address or '',
            'permanent_address': applicant.permanent_address or '',
        },
        'documents': [_serialize_document(d) for d in applicant.selection_form_document_ids],
    }


def _get_applicant_or_404(applicant_id):
    applicant = request.env['hr.applicant'].sudo().browse(applicant_id)
    if not applicant.exists():
        return None
    return applicant


def _write_partial(applicant, whitelist, kwargs):
    payload = _coerce(_pick(kwargs, whitelist))
    if not payload:
        return False
    if applicant.selection_form_status in ('approved',):
        raise UserError(
            'Cannot modify an approved selection form. '
            'Ask HR to re-open it before editing.'
        )
    applicant.write(payload)
    return True


class ApplicantSelectionFormDataController(http.Controller):

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form',
        type='http', auth='none',
        methods=['GET'], csrf=False, cors='*',
    )
    @validate_request({})
    def get_selection_form(self, applicant_id, **kwargs):
        try:
            applicant = _get_applicant_or_404(applicant_id)
            if not applicant:
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )
            mask = (kwargs.get('unmask') or '').strip().lower() not in (
                '1', 'true', 'yes',
            )
            return return_Response(
                'ok', 200,
                data={'record': _serialize_applicant(applicant, mask=mask)},
            )
        except Exception as exc:
            _logger.exception('get_selection_form failed')
            return return_Response(
                'Internal error', 500, errors=[str(exc)],
            )

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/employment',
        type='http', auth='none',
        methods=['PATCH'], csrf=False, cors='*',
    )
    @validate_request({})
    def patch_employment(self, applicant_id, **kwargs):
        return self._patch_section(
            applicant_id, EMPLOYMENT_FIELDS, kwargs,
        )

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/professional',
        type='http', auth='none',
        methods=['PATCH'], csrf=False, cors='*',
    )
    @validate_request({})
    def patch_professional(self, applicant_id, **kwargs):
        return self._patch_section(
            applicant_id, PROFESSIONAL_FIELDS, kwargs,
        )

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/family',
        type='http', auth='none',
        methods=['PATCH'], csrf=False, cors='*',
    )
    @validate_request({})
    def patch_family(self, applicant_id, **kwargs):
        return self._patch_section(
            applicant_id, FAMILY_FIELDS, kwargs,
        )

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/bank',
        type='http', auth='none',
        methods=['PATCH'], csrf=False, cors='*',
    )
    @validate_request({})
    def patch_bank(self, applicant_id, **kwargs):
        return self._patch_section(
            applicant_id, BANK_FIELDS, kwargs,
        )

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/address',
        type='http', auth='none',
        methods=['PATCH'], csrf=False, cors='*',
    )
    @validate_request({})
    def patch_address(self, applicant_id, **kwargs):
        return self._patch_section(
            applicant_id, ADDRESS_FIELDS, kwargs,
        )

    def _patch_section(self, applicant_id, whitelist, kwargs):
        try:
            applicant = _get_applicant_or_404(applicant_id)
            if not applicant:
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )
            _write_partial(applicant, whitelist, _body(kwargs))
            return return_Response(
                'Section saved', 200,
                data={'record': _serialize_applicant(applicant, mask=True)},
            )
        except (UserError, ValidationError) as exc:
            return return_Response('Validation failed', 400, errors=[str(exc)])
        except Exception as exc:
            _logger.exception('_patch_section failed')
            return return_Response('Internal error', 500, errors=[str(exc)])

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/references',
        type='http', auth='none',
        methods=['PUT'], csrf=False, cors='*',
    )
    @validate_request({})
    def put_references(self, applicant_id, **kwargs):
        try:
            applicant = _get_applicant_or_404(applicant_id)
            if not applicant:
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )
            if applicant.selection_form_status == 'approved':
                return return_Response(
                    'Approved selection forms are read-only', 400,
                    errors=['selection_form_status is approved'],
                )
            refs = _body(kwargs).get('references')
            if refs is None:
                return return_Response(
                    'references field is required', 400,
                    errors=['payload must contain a "references" array'],
                )
            if not isinstance(refs, list):
                return return_Response(
                    'references must be an array', 400,
                    errors=[f'got {type(refs).__name__}'],
                )
            if len(refs) > 2:
                return return_Response(
                    'A maximum of 2 references is allowed', 400,
                    errors=[f'received {len(refs)} references'],
                )

            commands = [(5, 0, 0)]
            for idx, r in enumerate(refs):
                if not isinstance(r, dict):
                    return return_Response(
                        'reference entries must be objects', 400,
                        errors=[f'index {idx} is {type(r).__name__}'],
                    )
                name = (r.get('name') or '').strip()
                email = (r.get('email') or '').strip()
                phone = (r.get('phone') or '').strip()
                if not name or not email or not phone:
                    return return_Response(
                        'name, email and phone are required per reference',
                        400,
                        errors=[f'reference #{idx + 1} missing required field'],
                    )
                commands.append((0, 0, {
                    'sequence': (idx + 1) * 10,
                    'name': name,
                    'email': email,
                    'phone': phone,
                    'linkedin_url': (r.get('linkedin_url') or '').strip() or False,
                }))
            applicant.write({'reference_ids': commands})
            return return_Response(
                'References saved', 200,
                data={'record': _serialize_applicant(applicant, mask=True)},
            )
        except (UserError, ValidationError) as exc:
            return return_Response('Validation failed', 400, errors=[str(exc)])
        except Exception as exc:
            _logger.exception('put_references failed')
            return return_Response('Internal error', 500, errors=[str(exc)])

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/submit',
        type='http', auth='none',
        methods=['POST'], csrf=False, cors='*',
    )
    @validate_request({})
    def submit_selection_form(self, applicant_id, **kwargs):
        try:
            applicant = _get_applicant_or_404(applicant_id)
            if not applicant:
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )
            if applicant.selection_form_status in ('submitted', 'under_review', 'approved'):
                return return_Response(
                    'Selection form already submitted', 400,
                    errors=[f'current status: {applicant.selection_form_status}'],
                )

            missing_fields = []
            for f in REQUIRED_ON_SUBMIT_BASE:
                if not applicant[f]:
                    missing_fields.append(f)

            if applicant.experience_type == 'experienced':
                for f in ('employer_1_name', 'employer_1_designation',
                          'employer_1_start_date'):
                    if not applicant[f]:
                        missing_fields.append(f)

            if applicant.has_uan_number and not applicant.uan_number:
                missing_fields.append('uan_number')

            if len(applicant.reference_ids) < 2:
                missing_fields.append('reference_ids (need 2)')

            uploaded_types = set(
                applicant.selection_form_document_ids.mapped('document_type'),
            )
            required_docs = list(REQUIRED_DOCUMENTS_BASE)
            if applicant.experience_type == 'experienced':
                required_docs.extend(REQUIRED_DOCUMENTS_EXPERIENCED)
            missing_docs = [d for d in required_docs if d not in uploaded_types]

            if missing_fields or missing_docs:
                return return_Response(
                    'Selection form incomplete', 400,
                    errors=[
                        *(f'missing field: {f}' for f in missing_fields),
                        *(f'missing document: {d}' for d in missing_docs),
                    ],
                )

            applicant.write({
                'selection_form_status': 'submitted',
                'selection_form_submitted_at': odoo_fields.Datetime.now(),
            })
            return return_Response(
                'Selection form submitted', 200,
                data={'record': _serialize_applicant(applicant, mask=True)},
            )
        except (UserError, ValidationError) as exc:
            return return_Response('Validation failed', 400, errors=[str(exc)])
        except Exception as exc:
            _logger.exception('submit_selection_form failed')
            return return_Response('Internal error', 500, errors=[str(exc)])

    @http.route(
        '/api/v1/hrms/applicant/<int:applicant_id>/selection-form/review',
        type='http', auth='none',
        methods=['POST'], csrf=False, cors='*',
    )
    @validate_request({})
    def review_selection_form(self, applicant_id, **kwargs):
        try:
            applicant = _get_applicant_or_404(applicant_id)
            if not applicant:
                return return_Response(
                    'Applicant not found', 404,
                    errors=[f'no hr.applicant with id={applicant_id}'],
                )
            data = _body(kwargs)
            decision = (data.get('decision') or '').strip().lower()
            if decision not in ('approved', 'rejected', 'under_review'):
                return return_Response(
                    'Invalid decision', 400,
                    errors=[
                        'decision must be one of: approved, rejected, under_review',
                    ],
                )
            if applicant.selection_form_status not in (
                'submitted', 'under_review',
            ):
                return return_Response(
                    'Selection form is not awaiting review', 400,
                    errors=[f'current status: {applicant.selection_form_status}'],
                )
            reason = (data.get('rejection_reason') or '').strip()
            if decision == 'rejected' and not reason:
                return return_Response(
                    'rejection_reason is required when decision=rejected',
                    400, errors=['missing rejection_reason'],
                )

            vals = {
                'selection_form_status': decision,
                'selection_form_reviewed_at': odoo_fields.Datetime.now(),
                'selection_form_reviewed_by_id': request.env.uid,
            }
            if decision == 'rejected':
                vals['selection_form_rejection_reason'] = reason
            else:
                vals['selection_form_rejection_reason'] = False
            applicant.write(vals)
            return return_Response(
                'Review recorded', 200,
                data={'record': _serialize_applicant(applicant, mask=True)},
            )
        except (UserError, ValidationError) as exc:
            return return_Response('Validation failed', 400, errors=[str(exc)])
        except Exception as exc:
            _logger.exception('review_selection_form failed')
            return return_Response('Internal error', 500, errors=[str(exc)])
