import re

from odoo import api, fields, models


PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


SCORE_TYPE_SELECTION = [
    ("percentage", "Percentage"),
    ("cgpa", "CGPA"),
    ("grade", "Grade"),
]

QUALIFICATION_SELECTION = [
    ("high_school", "High School"),
    ("intermediate", "Intermediate / 12th"),
    ("diploma", "Diploma"),
    ("bachelors", "Bachelor's Degree"),
    ("masters", "Master's Degree"),
    ("phd", "PhD"),
    ("other", "Other"),
]

BLOOD_GROUP_SELECTION = [
    ("a_pos", "A+"),
    ("a_neg", "A-"),
    ("b_pos", "B+"),
    ("b_neg", "B-"),
    ("ab_pos", "AB+"),
    ("ab_neg", "AB-"),
    ("o_pos", "O+"),
    ("o_neg", "O-"),
]

ONBOARDING_STATUS = [
    ("draft", "Draft"),
    ("submitted", "Submitted"),
]

# Fields that must be filled for the form to count as "completed".
# Mirrors every starred (*) field across the 6 form steps in the screenshots.
ONBOARDING_REQUIRED_FIELDS = (
    # Step 1 - Employment
    "employee_code", "name", "department_id", "designation_id",
    "birthday", "sex", "private_phone", "blood_group",
    "private_email", "work_email",
    # Step 2 - Family
    "marital",
    "father_name", "father_dob", "mother_name", "mother_dob",
    "emergency_contact", "emergency_phone", "emergency_contact_relation",
    # Step 3 - Education
    "tenth_score_type", "tenth_score",
    "twelfth_score_type", "twelfth_score",
    "highest_qualification",
    "highest_qualification_score_type", "highest_qualification_score",
    # Step 4 - Identity
    "aadhaar_number", "pan_number",
    # Step 5 - Bank
    "bank_account_number", "bank_name", "bank_ifsc_code",
    # Step 6 - Address
    "current_address", "permanent_address",
)

# Document slots required for completion. current_address_proof is optional
# because the form labels it "Optional when current address is same as permanent".
ONBOARDING_REQUIRED_DOCUMENTS = frozenset({
    "resume",
    "passport_photo",
    "tenth_marksheet",
    "twelfth_marksheet",
    "highest_qualification_certificate",
    "aadhaar_card",
    "pan_card",
    "cancelled_cheque",
    "permanent_address_proof",
})


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    # ------------------------------------------------------------------ #
    # Step 1 - Employment (uses existing keys where possible)
    #   Reused: name, employee_code, department_id, designation_id,
    #   birthday, sex, private_phone, private_email, work_email
    # ------------------------------------------------------------------ #
    blood_group = fields.Selection(
        BLOOD_GROUP_SELECTION,
        string="Blood Group",
        groups="hr.group_hr_user",
        tracking=True,
    )

    # ------------------------------------------------------------------ #
    # Step 2 - Family (reused: marital, emergency_contact, emergency_phone)
    # ------------------------------------------------------------------ #
    has_kids = fields.Boolean(
        string="Has Kids",
        groups="hr.group_hr_user",
        tracking=True,
    )
    father_name = fields.Char(
        string="Father's Name",
        groups="hr.group_hr_user",
        tracking=True,
    )
    father_dob = fields.Date(
        string="Father's DOB",
        groups="hr.group_hr_user",
        tracking=True,
    )
    mother_name = fields.Char(
        string="Mother's Name",
        groups="hr.group_hr_user",
        tracking=True,
    )
    mother_dob = fields.Date(
        string="Mother's DOB",
        groups="hr.group_hr_user",
        tracking=True,
    )
    emergency_contact_relation = fields.Char(
        string="Emergency Contact Relation",
        groups="hr.group_hr_user",
        tracking=True,
    )

    # ------------------------------------------------------------------ #
    # Step 3 - Education
    # ------------------------------------------------------------------ #
    tenth_score_type = fields.Selection(
        SCORE_TYPE_SELECTION,
        string="10th Score Type",
        groups="hr.group_hr_user",
    )
    tenth_score = fields.Char(
        string="10th Score",
        groups="hr.group_hr_user",
        help="Up to 5 characters, e.g. 10/2.",
    )
    twelfth_score_type = fields.Selection(
        SCORE_TYPE_SELECTION,
        string="12th / Diploma Score Type",
        groups="hr.group_hr_user",
    )
    twelfth_score = fields.Char(
        string="12th / Diploma Score",
        groups="hr.group_hr_user",
    )
    highest_qualification = fields.Selection(
        QUALIFICATION_SELECTION,
        string="Highest Qualification",
        groups="hr.group_hr_user",
    )
    highest_qualification_score_type = fields.Selection(
        SCORE_TYPE_SELECTION,
        string="Highest Qualification Score Type",
        groups="hr.group_hr_user",
    )
    highest_qualification_score = fields.Char(
        string="Highest Qualification Score",
        groups="hr.group_hr_user",
    )

    # ------------------------------------------------------------------ #
    # Step 4 - Identity
    #   Aadhaar reuses identification_id from hr.version; we also store
    #   a dedicated aadhaar_number for clarity since this form is
    #   India-specific.
    # ------------------------------------------------------------------ #
    aadhaar_number = fields.Char(
        string="Aadhaar Number",
        groups="hr.group_hr_user",
        tracking=True,
    )
    pan_number = fields.Char(
        string="PAN Number",
        groups="hr.group_hr_user",
        tracking=True,
    )
    has_uan = fields.Boolean(
        string="Has UAN Number",
        groups="hr.group_hr_user",
    )
    uan_number = fields.Char(
        string="UAN Number",
        groups="hr.group_hr_user",
        tracking=True,
    )

    # ------------------------------------------------------------------ #
    # Step 5 - Bank
    # ------------------------------------------------------------------ #
    has_savings_account = fields.Boolean(
        string="Has Savings Account",
        groups="hr.group_hr_user",
    )
    has_salary_account = fields.Boolean(
        string="Has Salary Account",
        groups="hr.group_hr_user",
    )
    bank_account_number = fields.Char(
        string="Bank Account Number",
        groups="hr.group_hr_user",
        tracking=True,
    )
    bank_name = fields.Char(
        string="Bank Name",
        groups="hr.group_hr_user",
        tracking=True,
    )
    bank_ifsc_code = fields.Char(
        string="IFSC Code",
        groups="hr.group_hr_user",
        tracking=True,
    )

    # ------------------------------------------------------------------ #
    # Step 6 - Address
    # ------------------------------------------------------------------ #
    current_address = fields.Text(
        string="Current Address",
        groups="hr.group_hr_user",
    )
    permanent_address = fields.Text(
        string="Permanent Address",
        groups="hr.group_hr_user",
    )
    current_same_as_permanent = fields.Boolean(
        string="Current Address Same as Permanent",
        groups="hr.group_hr_user",
        default=False,
    )

    # ------------------------------------------------------------------ #
    # Documents (One2many to keep all S3 URLs in one place)
    # ------------------------------------------------------------------ #
    onboarding_document_ids = fields.One2many(
        "employee.onboarding.document",
        "employee_id",
        string="Onboarding Documents",
    )

    # ------------------------------------------------------------------ #
    # Onboarding tracking
    # ------------------------------------------------------------------ #
    onboarding_status = fields.Selection(
        ONBOARDING_STATUS,
        string="Onboarding Status",
        default="draft",
        tracking=True,
    )
    onboarding_step = fields.Integer(
        string="Last Completed Onboarding Step",
        default=0,
        help="0 = not started, 1-6 = step number completed, 6 = full form submitted.",
    )
    onboarding_submitted_at = fields.Datetime(
        string="Onboarding Submitted At",
        readonly=True,
    )
    onboarding_completed = fields.Boolean(
        string="Onboarding Completed",
        compute="_compute_onboarding_completed",
        help="True only when every required onboarding field and document is present. "
             "Used by the login API to decide whether to show the onboarding popup.",
    )

    @api.depends(
        "onboarding_document_ids",
        "onboarding_document_ids.document_type",
        "has_uan", "uan_number",
        *ONBOARDING_REQUIRED_FIELDS,
    )
    def _compute_onboarding_completed(self):
        for emp in self:
            ok = True
            for fname in ONBOARDING_REQUIRED_FIELDS:
                if not emp[fname]:
                    ok = False
                    break
            if ok and emp.has_uan and not emp.uan_number:
                ok = False
            if ok:
                uploaded_types = set(emp.onboarding_document_ids.mapped("document_type"))
                if not ONBOARDING_REQUIRED_DOCUMENTS.issubset(uploaded_types):
                    ok = False
            emp.onboarding_completed = ok

    @api.onchange("aadhaar_number")
    def _onchange_aadhaar_number(self):
        value = (self.aadhaar_number or "").strip()
        if value and (not value.isdigit() or len(value) != 12):
            return {
                "warning": {
                    "title": "Invalid Aadhaar Number",
                    "message": "Aadhaar Number must be exactly 12 digits.",
                }
            }

    @api.onchange("pan_number")
    def _onchange_pan_number(self):
        value = (self.pan_number or "").strip().upper()
        if value and not PAN_PATTERN.match(value):
            return {
                "warning": {
                    "title": "Invalid PAN Number",
                    "message": "PAN Number must follow the format AAAAA9999A (5 letters, 4 digits, 1 letter).",
                }
            }

    @api.onchange("uan_number")
    def _onchange_uan_number(self):
        value = (self.uan_number or "").strip()
        if value and (not value.isdigit() or len(value) != 12):
            return {
                "warning": {
                    "title": "Invalid UAN Number",
                    "message": "UAN Number must be exactly 12 digits.",
                }
            }
