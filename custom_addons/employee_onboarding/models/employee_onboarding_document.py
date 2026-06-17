from odoo import api, fields, models


DOCUMENT_TYPES = [
    ("resume", "Resume"),
    ("passport_photo", "Passport Size Photo"),
    ("tenth_marksheet", "10th Marksheet / Certificate"),
    ("twelfth_marksheet", "12th / Diploma Marksheet / Certificate"),
    ("highest_qualification", "Highest Qualification Certificate"),
    ("aadhaar_card", "Aadhaar Card"),
    ("pan_card", "PAN Card"),
    ("cancelled_cheque", "Cancelled Cheque / Passbook"),
    ("permanent_address_proof", "Permanent Address Proof"),
    ("current_address_proof", "Current Address Proof"),
]


class EmployeeOnboardingDocument(models.Model):
    _name = "employee.onboarding.document"
    _description = "Employee Onboarding Document"
    _order = "employee_id, document_type"

    employee_id = fields.Many2one(
        "hr.employee",
        string="Employee",
        required=True,
        ondelete="cascade",
        index=True,
    )
    document_type = fields.Selection(
        DOCUMENT_TYPES,
        string="Document Type",
        required=True,
    )
    document_label = fields.Char(
        string="Document Label",
        compute="_compute_document_label",
        store=True,
    )
    file_name = fields.Char(string="File Name")
    file_url = fields.Char(string="File URL", required=True)
    uploaded_at = fields.Datetime(
        string="Uploaded At",
        default=fields.Datetime.now,
        readonly=True,
    )

    _sql_constraints = [
        (
            "uniq_employee_doc_type",
            "unique(employee_id, document_type)",
            "Each document type can only be uploaded once per employee. Replace the existing document instead.",
        ),
    ]

    @api.depends("document_type")
    def _compute_document_label(self):
        type_map = dict(DOCUMENT_TYPES)
        for rec in self:
            rec.document_label = type_map.get(rec.document_type, rec.document_type or "")
