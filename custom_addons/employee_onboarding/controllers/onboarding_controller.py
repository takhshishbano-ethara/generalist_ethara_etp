"""REST API for the 6-step Employee Onboarding form.

Endpoints:
  POST /api/v2/employee-onboarding/submit
       multipart/form-data. Flat text fields for all step data plus one file
       per document slot (field name == document_type). Files are pushed to S3
       and only the resulting CDN URL is persisted.

  GET  /api/v2/employee-onboarding/<int:employee_id>
       Return the employee's onboarding details plus every uploaded document
       with its S3 URL grouped by document_type.

  GET  /api/v2/employee-onboarding/<int:employee_id>/documents
       Return just the uploaded documents (with S3 URLs) for an employee.
"""

import base64
import logging
import mimetypes
import os
import re
import time
import uuid

from werkzeug.utils import secure_filename

from odoo import fields, http
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)
from odoo.addons.employee_extension.services.aadhaar_address import (
    extract_aadhaar_info_from_bytes,
)

_logger = logging.getLogger(__name__)


S3_PREFIX = "employee_onboarding"

PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

ALLOWED_DOCUMENT_TYPES = {
    "resume",
    "passport_photo",
    "tenth_marksheet",
    "twelfth_marksheet",
    "highest_qualification_certificate",
    "aadhaar_card",
    "pan_card",
    "cancelled_cheque",
    "permanent_address_proof",
    "current_address_proof",
}

AADHAAR_EXTRACT_ALLOWED_EXT = {"pdf", "jpg", "jpeg", "png", "webp"}
AADHAAR_EXTRACT_MAX_SIZE = 10 * 1024 * 1024

# Maps incoming gender strings to the hr.version `sex` selection values.
GENDER_MAP = {
    "male": "male",
    "m": "male",
    "female": "female",
    "f": "female",
    "other": "other",
    "o": "other",
}


def _truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("yes", "true", "1", "on")


def _str(value):
    return str(value).strip() if value not in (None, "") else None


def _normalize_gender(value):
    if not value:
        return False
    return GENDER_MAP.get(str(value).strip().lower(), False)


def _validate_identity_keys(params):
    errors = []

    aadhaar = params.get("aadhaar_number")
    if aadhaar not in (None, ""):
        value = str(aadhaar).strip()
        if not value.isdigit() or len(value) != 12:
            errors.append("Aadhaar Number must be exactly 12 digits.")

    pan = params.get("pan_number")
    if pan not in (None, ""):
        value = str(pan).strip().upper()
        if not PAN_PATTERN.match(value):
            errors.append(
                "PAN Number must follow the format AAAAA9999A (5 letters, 4 digits, 1 letter)."
            )

    uan = params.get("uan_number")
    if uan not in (None, ""):
        value = str(uan).strip()
        if not value.isdigit() or len(value) != 12:
            errors.append("UAN Number must be exactly 12 digits.")

    return errors


def _upload_to_s3(b64data, document_type, employee_id, original_filename=None):
    """Push base64 bytes to S3 under the onboarding prefix and return the URL.

    The S3 key is built as:
        employee_onboarding/<employee_id>/<document_type>/<timestamp>_<uuid>_<safe_name>.<ext>
    """
    connector = request.env["s3.connector"].sudo().search([], limit=1)
    if not connector:
        return False, "No S3 connector is configured."

    ts = time.time_ns()
    unique_id = uuid.uuid4().hex[:12]

    if original_filename:
        _root, ext = os.path.splitext(original_filename)
        if not ext:
            mime = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
            ext = mimetypes.guess_extension(mime) or ".bin"
        base_name = secure_filename(os.path.splitext(original_filename)[0]) or document_type
    else:
        ext = ".bin"
        base_name = document_type

    safe_name = f"{ts}_{unique_id}_{base_name}{ext}"

    try:
        wizard = request.env["s3.upload.wizard"].sudo().create({
            "s3_connector_id": connector.id,
            "upload_file": b64data,
            "prefix": f"{S3_PREFIX}/{employee_id}/{document_type}",
            "file_name": safe_name,
        })
        url = wizard.upload_images_in_s3_get_url()
    except Exception as exc:
        _logger.exception("Onboarding S3 upload failed for type=%s", document_type)
        return False, f"S3 upload failed: {exc}"

    if not url:
        return False, "S3 upload returned no URL."
    return url, None


def _save_files(employee, files):
    """Persist every file in the multipart payload whose field name matches
    one of ALLOWED_DOCUMENT_TYPES. Files for unknown field names are ignored.
    """
    if not files:
        return [], []

    Document = request.env["employee.onboarding.document"].sudo()
    uploaded, errors = [], []

    for field_name in files.keys():
        if field_name not in ALLOWED_DOCUMENT_TYPES:
            continue
        file_storage = files.get(field_name)
        if not file_storage or not getattr(file_storage, "filename", None):
            continue

        try:
            file_bytes = file_storage.read()
        except Exception as exc:
            errors.append({"document_type": field_name, "error": f"Failed to read upload: {exc}"})
            continue
        if not file_bytes:
            errors.append({"document_type": field_name, "error": "Uploaded file is empty."})
            continue

        b64data = base64.b64encode(file_bytes).decode("ascii")
        original_filename = file_storage.filename

        url, err = _upload_to_s3(b64data, field_name, employee.id, original_filename)
        if err:
            errors.append({"document_type": field_name, "error": err})
            continue

        existing = Document.search([
            ("employee_id", "=", employee.id),
            ("document_type", "=", field_name),
        ], limit=1)
        now = fields.Datetime.now()
        if existing:
            existing.write({
                "file_name": original_filename,
                "file_url": url,
                "uploaded_at": now,
            })
            record = existing
        else:
            record = Document.create({
                "employee_id": employee.id,
                "document_type": field_name,
                "file_name": original_filename,
                "file_url": url,
                "uploaded_at": now,
            })

        uploaded.append({
            "id": record.id,
            "document_type": record.document_type,
            "document_label": record.document_label,
            "file_name": record.file_name,
            "file_url": record.file_url,
            "uploaded_at": record.uploaded_at.isoformat() if record.uploaded_at else None,
        })

    return uploaded, errors


def _vals_from_params(params):
    """Map flat form-data field names to hr.employee write values.

    Field name aliases tolerated by the API:
      personal_email | private_email           -> private_email
      official_email | work_email              -> work_email
      contact_number | private_phone           -> private_phone
      date_of_birth  | birthday                -> birthday
      gender         | sex                     -> sex
      marital_status | marital                 -> marital
      emergency_contact_name | emergency_contact   -> emergency_contact
      emergency_contact_number | emergency_phone   -> emergency_phone
      bank_account | bank_account_number       -> bank_account_number
      ifsc_code    | bank_ifsc_code            -> bank_ifsc_code
    """
    vals = {}

    # Step 1 - Employment
    if params.get("employee_code"):
        vals["employee_code"] = _str(params["employee_code"])
    if params.get("name"):
        vals["name"] = _str(params["name"])
    if params.get("department_id"):
        vals["department_id"] = int(params["department_id"])
    if params.get("designation_id"):
        vals["designation_id"] = int(params["designation_id"])
    dob = params.get("date_of_birth") or params.get("birthday")
    if dob:
        vals["birthday"] = dob
    gender = _normalize_gender(params.get("gender") or params.get("sex"))
    if gender:
        vals["sex"] = gender
    phone = params.get("contact_number") or params.get("private_phone")
    if phone:
        vals["private_phone"] = _str(phone)
    if params.get("blood_group"):
        vals["blood_group"] = params["blood_group"]
    p_email = params.get("personal_email") or params.get("private_email")
    if p_email:
        vals["private_email"] = _str(p_email).lower()
    o_email = params.get("official_email") or params.get("work_email")
    if o_email:
        vals["work_email"] = _str(o_email).lower()

    # Step 2 - Family
    marital = params.get("marital_status") or params.get("marital")
    if marital:
        vals["marital"] = marital
    if "has_kids" in params:
        vals["has_kids"] = _truthy(params["has_kids"])
    if params.get("father_name"):
        vals["father_name"] = _str(params["father_name"])
    if params.get("father_dob"):
        vals["father_dob"] = params["father_dob"]
    if params.get("mother_name"):
        vals["mother_name"] = _str(params["mother_name"])
    if params.get("mother_dob"):
        vals["mother_dob"] = params["mother_dob"]
    e_name = params.get("emergency_contact_name") or params.get("emergency_contact")
    if e_name:
        vals["emergency_contact"] = _str(e_name)
    e_phone = params.get("emergency_contact_number") or params.get("emergency_phone")
    if e_phone:
        vals["emergency_phone"] = _str(e_phone)
    if params.get("emergency_contact_relation"):
        vals["emergency_contact_relation"] = _str(params["emergency_contact_relation"])

    # Step 3 - Education
    for key in (
        "tenth_score_type", "tenth_score",
        "twelfth_score_type", "twelfth_score",
        "highest_qualification",
        "highest_qualification_score_type", "highest_qualification_score",
    ):
        if params.get(key) not in (None, ""):
            value = params[key]
            vals[key] = _str(value) if isinstance(value, str) else value

    # Step 4 - Identity
    if params.get("aadhaar_number"):
        aadhaar = _str(params["aadhaar_number"])
        vals["aadhaar_number"] = aadhaar
        vals["identification_id"] = aadhaar
    if params.get("pan_number"):
        vals["pan_number"] = _str(params["pan_number"]).upper()
    if "has_uan" in params:
        vals["has_uan"] = _truthy(params["has_uan"])
    if params.get("uan_number"):
        vals["uan_number"] = _str(params["uan_number"])

    # Step 5 - Bank
    if "has_savings_account" in params:
        vals["has_savings_account"] = _truthy(params["has_savings_account"])
    if "has_salary_account" in params:
        vals["has_salary_account"] = _truthy(params["has_salary_account"])
    bank_acc = params.get("bank_account_number") or params.get("bank_account")
    if bank_acc:
        vals["bank_account_number"] = _str(bank_acc)
    if params.get("bank_name"):
        vals["bank_name"] = _str(params["bank_name"])
    ifsc = params.get("ifsc_code") or params.get("bank_ifsc_code")
    if ifsc:
        vals["bank_ifsc_code"] = _str(ifsc).upper()

    # Step 6 - Address
    if params.get("current_address"):
        vals["current_address"] = _str(params["current_address"])
    if params.get("permanent_address"):
        vals["permanent_address"] = _str(params["permanent_address"])
    if "current_same_as_permanent" in params:
        vals["current_same_as_permanent"] = _truthy(params["current_same_as_permanent"])

    return vals


def _serialize_employee(employee):
    """Return the full employee onboarding payload for the GET endpoint."""
    docs_by_type = {}
    for doc in employee.onboarding_document_ids:
        docs_by_type[doc.document_type] = {
            "id": doc.id,
            "document_type": doc.document_type,
            "document_label": doc.document_label,
            "file_name": doc.file_name,
            "file_url": doc.file_url,
            "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
        }

    return {
        "employee_id": employee.id,
        "onboarding": {
            "status": employee.onboarding_status,
            "step": employee.onboarding_step,
            "submitted_at": employee.onboarding_submitted_at.isoformat() if employee.onboarding_submitted_at else None,
        },
        "employment": {
            "employee_code": employee.employee_code or None,
            "name": employee.name or None,
            "department_id": employee.department_id.id or None,
            "department_name": employee.department_id.name or None,
            "designation_id": employee.designation_id.id if employee.designation_id else None,
            "designation_name": employee.designation_id.name if employee.designation_id else None,
            "date_of_birth": employee.birthday.isoformat() if employee.birthday else None,
            "gender": employee.sex or None,
            "contact_number": employee.private_phone or employee.work_phone or None,
            "blood_group": employee.blood_group or None,
            "personal_email": employee.private_email or None,
            "official_email": employee.work_email or None,
        },
        "family": {
            "marital_status": employee.marital or None,
            "has_kids": employee.has_kids,
            "father_name": employee.father_name or None,
            "father_dob": employee.father_dob.isoformat() if employee.father_dob else None,
            "mother_name": employee.mother_name or None,
            "mother_dob": employee.mother_dob.isoformat() if employee.mother_dob else None,
            "emergency_contact_name": employee.emergency_contact or None,
            "emergency_contact_number": employee.emergency_phone or None,
            "emergency_contact_relation": employee.emergency_contact_relation or None,
        },
        "education": {
            "tenth_score_type": employee.tenth_score_type or None,
            "tenth_score": employee.tenth_score or None,
            "twelfth_score_type": employee.twelfth_score_type or None,
            "twelfth_score": employee.twelfth_score or None,
            "highest_qualification": employee.highest_qualification or None,
            "highest_qualification_score_type": employee.highest_qualification_score_type or None,
            "highest_qualification_score": employee.highest_qualification_score or None,
        },
        "identity": {
            "aadhaar_number": employee.aadhaar_number or employee.identification_id or None,
            "pan_number": employee.pan_number or None,
            "has_uan": employee.has_uan,
            "uan_number": employee.uan_number or None,
        },
        "bank": {
            "has_savings_account": employee.has_savings_account,
            "has_salary_account": employee.has_salary_account,
            "bank_account_number": employee.bank_account_number or None,
            "bank_name": employee.bank_name or None,
            "ifsc_code": employee.bank_ifsc_code or None,
        },
        "address": {
            "current_address": employee.current_address or None,
            "permanent_address": employee.permanent_address or None,
            "current_same_as_permanent": employee.current_same_as_permanent,
        },
        "documents": [docs_by_type[k] for k in sorted(docs_by_type.keys())],
        "documents_by_type": docs_by_type,
    }


class EmployeeOnboardingController(http.Controller):

    @http.route(
        "/api/v2/employee-onboarding/extract_address",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def extract_address_from_proof(self, **kwargs):
        files = request.httprequest.files
        uploaded = files.get("file") or (
            next(iter(files.values())) if files else None
        )
        if not uploaded or not uploaded.filename:
            return return_Response(
                message="No file uploaded.",
                status=400,
            )

        filename = secure_filename(uploaded.filename) or "upload"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in AADHAAR_EXTRACT_ALLOWED_EXT:
            return return_Response(
                message=(
                    f"Unsupported file type: {ext}. Allowed: "
                    f"{', '.join(sorted(AADHAAR_EXTRACT_ALLOWED_EXT))}."
                ),
                status=400,
            )

        try:
            file_bytes = uploaded.read()
        except Exception as e:
            _logger.warning("extract_address: read failed: %s", e)
            return return_Response(
                message="Could not read uploaded file.",
                status=400,
            )
        if not file_bytes:
            return return_Response(
                message="Uploaded file is empty.",
                status=400,
            )
        if len(file_bytes) > AADHAAR_EXTRACT_MAX_SIZE:
            return return_Response(
                message="File too large. Max 10 MB.",
                status=400,
            )

        try:
            info = extract_aadhaar_info_from_bytes(file_bytes, filename) or {}
        except Exception as e:
            _logger.exception("extract_address: extraction failed: %s", e)
            info = {}

        address = (info.get("address") or "").strip()
        last4 = info.get("aadhaar_last4") or ""
        is_aadhaar = bool(last4) or bool(address)
        is_masked = bool(info.get("is_masked"))

        _logger.info(
            "extract_address: is_aadhaar=%s last4=%s addr_found=%s",
            is_aadhaar, last4, bool(address),
        )

        return return_Response(
            message="Address extracted.",
            data={
                "ok": True,
                "is_aadhaar": is_aadhaar,
                "is_masked": is_masked,
                "address": address,
                "aadhaar_last4": last4,
            },
        )

    @http.route(
        "/api/v2/employee-onboarding/submit",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def submit_onboarding(self, **kwargs):
        """Create or update an employee onboarding record.

        Send as multipart/form-data:

          Text fields (any subset):
            employee_id, step, final_submit
            employee_code, name, department_id, designation_id, date_of_birth,
            gender, contact_number, blood_group, personal_email, official_email,
            marital_status, has_kids, father_name, father_dob, mother_name,
            mother_dob, emergency_contact_name, emergency_contact_number,
            emergency_contact_relation,
            tenth_score_type, tenth_score, twelfth_score_type, twelfth_score,
            highest_qualification, highest_qualification_score_type,
            highest_qualification_score,
            aadhaar_number, pan_number, has_uan, uan_number,
            has_savings_account, has_salary_account, bank_account_number,
            bank_name, ifsc_code,
            current_address, permanent_address, current_same_as_permanent

          File fields (each optional; field name == document_type):
            resume, passport_photo, tenth_marksheet, twelfth_marksheet,
            highest_qualification_certificate, aadhaar_card, pan_card,
            cancelled_cheque, permanent_address_proof, current_address_proof
        """
        try:
            params = dict(kwargs)
            files = request.httprequest.files

            Employee = request.env["hr.employee"].sudo()
            employee_id = params.pop("employee_id", None)
            employee = Employee.browse(int(employee_id)) if employee_id else Employee.browse()
            if employee_id and not employee.exists():
                return return_Response(message=f"Employee {employee_id} not found.", status=404)

            step = params.pop("step", None)
            final_submit = _truthy(params.pop("final_submit", None))

            identity_errors = _validate_identity_keys(params)
            if identity_errors:
                return return_Response(
                    message="Invalid identity details.",
                    status=400,
                    errors=identity_errors,
                )

            vals = _vals_from_params(params)

            if not employee:
                if not vals.get("name"):
                    return return_Response(
                        message="'name' is required to create a new employee.",
                        status=400,
                    )
                employee = Employee.create(vals)
            elif vals:
                employee.write(vals)

            uploaded, doc_errors = _save_files(employee, files)

            try:
                step_int = int(step) if step is not None else None
            except (TypeError, ValueError):
                step_int = None
            tracking_vals = {}
            if step_int and 1 <= step_int <= 6 and step_int > (employee.onboarding_step or 0):
                tracking_vals["onboarding_step"] = step_int
            if final_submit:
                tracking_vals["onboarding_status"] = "submitted"
                tracking_vals["onboarding_step"] = 6
                tracking_vals["onboarding_submitted_at"] = fields.Datetime.now()
            if tracking_vals:
                employee.write(tracking_vals)

            response_data = {
                "employee_id": employee.id,
                "onboarding_status": employee.onboarding_status,
                "onboarding_step": employee.onboarding_step,
                "uploaded_documents": uploaded,
            }
            if doc_errors:
                response_data["document_errors"] = doc_errors
                return return_Response(
                    message="Onboarding saved with some document errors.",
                    status=200,
                    data=response_data,
                )
            return return_Response(
                message="Onboarding details saved successfully.",
                status=200,
                data=response_data,
            )
        except Exception as exc:
            _logger.exception("Onboarding submit failed")
            return return_Response(message=str(exc), status=400)

    @http.route(
        "/api/v2/employee-onboarding/<int:employee_id>",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def get_onboarding(self, employee_id, **kwargs):
        """Return the employee's onboarding payload + all S3 document URLs."""
        try:
            employee = request.env["hr.employee"].sudo().browse(employee_id)
            if not employee.exists():
                return return_Response(message=f"Employee {employee_id} not found.", status=404)
            return return_Response(
                message="Employee onboarding details fetched successfully.",
                status=200,
                data=_serialize_employee(employee),
            )
        except Exception as exc:
            _logger.exception("Onboarding fetch failed for employee_id=%s", employee_id)
            return return_Response(message=str(exc), status=400)

    @http.route(
        "/api/v1/get_employee_department",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def get_employee_department(self, **kwargs):
        """Return every active hr.department for populating the
        onboarding form's Department dropdown."""
        try:
            departments = request.env["hr.department"].sudo().search(
                [("active", "=", True)],
                order="parent_path asc, name asc",
            )
            records = [
                {
                    "id": dept.id,
                    "name": dept.name,
                    "complete_name": dept.complete_name,
                    "parent_id": dept.parent_id.id if dept.parent_id else None,
                    "parent_name": dept.parent_id.name if dept.parent_id else None,
                }
                for dept in departments
            ]
            return return_Response(
                message="Success",
                status=200,
                data={"record": records, "total_record_count": len(records)},
            )
        except Exception as exc:
            _logger.exception("Failed to fetch hr.department list")
            return return_Response(message="Fetch Failed", status=400, errors=[str(exc)])

    @http.route(
        "/api/v2/employee-onboarding/<int:employee_id>/documents",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    @validate_token
    def get_onboarding_documents(self, employee_id, **kwargs):
        """Return just the uploaded documents (with S3 URLs) for an employee."""
        try:
            employee = request.env["hr.employee"].sudo().browse(employee_id)
            if not employee.exists():
                return return_Response(message=f"Employee {employee_id} not found.", status=404)
            payload = [
                {
                    "id": doc.id,
                    "document_type": doc.document_type,
                    "document_label": doc.document_label,
                    "file_name": doc.file_name,
                    "file_url": doc.file_url,
                    "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
                }
                for doc in employee.onboarding_document_ids
            ]
            return return_Response(
                message="Employee documents fetched successfully.",
                status=200,
                data={
                    "employee_id": employee.id,
                    "employee_name": employee.name,
                    "documents": payload,
                    "document_count": len(payload),
                },
            )
        except Exception as exc:
            _logger.exception("Onboarding documents fetch failed for employee_id=%s", employee_id)
            return return_Response(message=str(exc), status=400)
