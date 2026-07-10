import logging
from datetime import datetime, time

from odoo import http, fields
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    safe_get_value,
)
from odoo.addons.api_auth_gateway.controllers.main import validate_request

_logger = logging.getLogger(__name__)


BASIC_PROFILE_FIELDS = ("work_email", "aadhaar_number", "private_phone")

DETAIL_FORM_FIELDS = (
    "current_address", "permanent_address",
    "aadhaar_number", "pan_number",
    "highest_qualification", "tenth_score", "twelfth_score",
)

PROFILE_ALL_FIELDS = (
    "employee_code", "name", "department_id", "designation_id",
    "birthday", "sex", "private_phone", "blood_group",
    "private_email", "work_email",
    "marital",
    "father_name", "father_dob", "mother_name", "mother_dob",
    "emergency_contact", "emergency_phone", "emergency_contact_relation",
    "tenth_score_type", "tenth_score",
    "twelfth_score_type", "twelfth_score",
    "highest_qualification",
    "highest_qualification_score_type", "highest_qualification_score",
    "aadhaar_number", "pan_number",
    "bank_account_number", "bank_name", "bank_ifsc_code",
    "current_address", "permanent_address",
)

REQUIRED_DOCUMENT_TYPES = frozenset({
    "resume", "passport_photo", "tenth_marksheet", "twelfth_marksheet",
    "highest_qualification_certificate", "aadhaar_card", "pan_card",
    "cancelled_cheque", "permanent_address_proof",
})

TOTAL_DOCUMENT_TYPES = 10


def _parse_int(v):
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _iso_date(d):
    if not d:
        return None
    if isinstance(d, str):
        return d
    return d.strftime('%Y-%m-%d')


def _all_filled(record, field_names):
    for fname in field_names:
        if fname not in record._fields:
            return False
        if not record[fname]:
            return False
    return True


def _employee_status(emp):
    if 'offboarding_state' in emp._fields and emp.offboarding_state:
        state = emp.offboarding_state
        if state == 'active':
            return 'Active'
        if state == 'offboarding':
            return 'Offboarding'
        if state == 'offboarded':
            return 'Offboarded'
    return 'Active' if emp.active else 'Inactive'


def _today_attendance_status(emp):
    today = fields.Date.context_today(emp)
    start = datetime.combine(today, time.min)
    end = datetime.combine(today, time.max)
    attendance = request.env['hr.attendance'].sudo().search([
        ('employee_id', '=', emp.id),
        ('check_in', '>=', start),
        ('check_in', '<=', end),
    ], order='check_in desc', limit=1)
    if not attendance:
        return 'Absent'
    if 'attendance_status' in attendance._fields and attendance.attendance_status:
        return str(attendance.attendance_status).capitalize()
    if attendance.check_out:
        return 'Checked Out'
    return 'Present'


def _serialize_document(doc):
    return {
        'document_name': safe_get_value(doc, 'document_label', 'str')
                         or safe_get_value(doc, 'file_name', 'str'),
        'link': safe_get_value(doc, 'file_url', 'str'),
        'status': 'Uploaded',
        'document_type': safe_get_value(doc, 'document_type', 'str'),
    }


def _build_onboarding_checklist(emp, uploaded_doc_types):
    basic_profile_completed = _all_filled(emp, BASIC_PROFILE_FIELDS)
    employee_detail_form_submitted = _all_filled(emp, DETAIL_FORM_FIELDS)
    document_uploaded = REQUIRED_DOCUMENT_TYPES.issubset(uploaded_doc_types)
    profile_completed = _all_filled(emp, PROFILE_ALL_FIELDS)
    return {
        'basic_profile_completed': basic_profile_completed,
        'employee_detail_form_submitted': employee_detail_form_submitted,
        'document_uploaded': document_uploaded,
        'contract_completion': False,
        'compliance_submitted': False,
        'referral_module_available': False,
        'profile_completed': profile_completed,
    }


def _resolve_join_date(emp):
    for fname in ('joining_date', 'first_contract_date'):
        if fname in emp._fields and emp[fname]:
            return _iso_date(emp[fname])
    return _iso_date(emp.create_date.date() if emp.create_date else None)


def _resolve_employee_code(emp):
    for fname in ('employee_code',):
        if fname in emp._fields and emp[fname]:
            return emp[fname]
    return None


class EtharaEmployeeDashboardController(http.Controller):

    @http.route('/api/v1/employee/dashboard', type='http', auth='none',
                methods=['GET', 'POST'], csrf=False, cors='*')
    @validate_request({})
    def employee_dashboard(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            params = {**kwargs, **jdata}

            employee_id = _parse_int(params.get('employee_id'))
            if not employee_id:
                return return_Response(
                    'employee_id is required', 400,
                    errors=['employee_id is required'],
                )

            emp = request.env['hr.employee'].sudo().browse(employee_id)
            if not emp.exists():
                return return_Response(
                    'Employee not found', 404,
                    errors=['employee_id %s does not exist' % employee_id],
                )

            documents = request.env['employee.onboarding.document'].sudo().search([
                ('employee_id', '=', emp.id),
            ])
            uploaded_doc_types = set(documents.mapped('document_type'))

            leave_types = request.env['greythr.leave.type'].sudo().search([])

            existing_balances = request.env['greythr.leave.balance'].sudo().search(
                [
                    ('employee_id', '=', emp.id),
                    ('leave_type_id', 'in', leave_types.ids),
                ],
                order='year desc, id desc',
            )
            balance_by_type = {}
            for bal in existing_balances:
                type_id = bal.leave_type_id.id if bal.leave_type_id else None
                if type_id and type_id not in balance_by_type:
                    balance_by_type[type_id] = bal

            leave_balance_records = {}
            for lt in leave_types:
                bal = balance_by_type.get(lt.id)
                leave_balance_records[lt.name] = (
                    safe_get_value(bal, 'current_balance', 'float') if bal else 0.0
                )

            onboarding_completed = bool(
                'onboarding_completed' in emp._fields and emp.onboarding_completed
            )

            uploaded_count = len(uploaded_doc_types)

            record = {
                'employee_id': emp.id,
                'employee_name': safe_get_value(emp, 'name', 'str'),
                'employee_code': _resolve_employee_code(emp),
                'designation': safe_get_value(emp, 'job_id.name', 'str') if emp.job_id else safe_get_value(emp, 'designation_id.name', 'str'),
                'department': safe_get_value(emp, 'department_id.name', 'str'),
                'join_date': _resolve_join_date(emp),
                'work_email': safe_get_value(emp, 'work_email', 'str'),
                'status': _employee_status(emp),
                'attendance_status': _today_attendance_status(emp),
                'onboarding_status': 'on-boarded' if onboarding_completed else 'Action needed',
                'contract_status': 'Draft',
                'leave_balance': leave_balance_records,
                'document_uploaded_count': '%d/%d' % (uploaded_count, TOTAL_DOCUMENT_TYPES),
                'compliance_status': 'Not Assigned',
                'referral_count': 0,
                'documents': [_serialize_document(d) for d in documents],
                'onboarding_checklist': _build_onboarding_checklist(emp, uploaded_doc_types),
                'contract_and_compliance': [],
            }

            return return_Response('OK', 200, data={'record': record})
        except Exception as exc:
            _logger.exception('employee_dashboard failed')
            return return_Response('Internal error', 500, errors=[str(exc)])
