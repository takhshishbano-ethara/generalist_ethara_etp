{
    "name": "Employee Onboarding",
    "version": "19.0.1.0.0",
    "category": "Human Resources",
    "summary": "6-step employee onboarding form with S3 document storage and REST APIs",
    "description": """
Employee Onboarding Module
==========================
Inherits hr.employee and adds the fields captured by the 6-step onboarding form:

1. Employment   - employee code, department, designation, DOB, gender, contact,
                  blood group, personal/official email, resume, passport photo.
2. Family       - marital status, kids flag, parents' names/DOBs, emergency
                  contact details and relation.
3. Education    - 10th, 12th/Diploma, highest qualification details with score
                  type, score and supporting certificate uploads.
4. Identity     - Aadhaar, PAN, UAN with card uploads.
5. Bank         - savings/salary account flags, account/IFSC/bank name and
                  cancelled cheque upload.
6. Address      - current and permanent addresses with proof uploads.

All documents are pushed to S3 via s3.connector and only the public CDN URL is
persisted. REST APIs are exposed under /api/v2/employee-onboarding for create
(multi-step) and get (full employee details including all uploaded documents).
    """,
    "author": "Ethara ERP Team",
    "website": "https://ethara.ai",
    "license": "LGPL-3",
    "depends": [
        "base",
        "hr",
        "mail",
        "api_auth_gateway",
        "s3_connector",
        "hr_employee_updation",
        "employee_role_import",
        "project_extension",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/employee_onboarding_document_views.xml",
        "views/hr_employee_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
