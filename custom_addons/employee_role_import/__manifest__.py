{
    "name": "Employee Role Import",
    "version": "19.0.3.2.0",
    "category": "Human Resources",
    "summary": "Bulk-import employees from CSV (UI wizard + REST API) and assign ETP roles.",
    "description": """
Employee Role Import
====================
Provides:

* A wizard that imports employees from a CSV file with the columns
  ``name``, ``employee_id``, ``email`` (required) plus optional
  ``role``, ``job_title``, ``assigned_ql``, ``assigned_pl``, ``assigned_tpm``
  and assigns an ETP role (TPM / PL / QL / QR / DM / CTO / HR Admin /
  IT Admin / Tasker) to every employee in the batch.

* Hierarchy mapping applied automatically based on role:
  Tasker / QR → Assigned QL + PL + TPM,
  QL → Assigned PL + TPM,
  PL → Assigned TPM,
  TPM / CTO → no hierarchy fields.

* A versioned REST API (``/api/v2/employee-role-import/...``) covering CSV
  upload, preview, save/import, employee CRUD, search & filter listing,
  CSV/XLSX export, role-hierarchy queries, and QL / PL / TPM picker
  endpoints.  Authenticated via the shared ``api.access_token`` issued by
  ``api_auth_gateway``.
    """,
    "author": "ERP Team",
    "website": "",
    "license": "LGPL-3",
    "depends": [
        "base",
        "hr",
        "etp_user_roles",
        "api_auth_gateway",
        "task_forge_bridge",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/employee_import_session_cron.xml",
        "views/hr_employee_views.xml",
        "views/employee_role_import_views.xml",
        "views/menu.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "employee_role_import/static/src/css/wizard.css",
        ],
    },
    "external_dependencies": {
        "python": ["xlsxwriter"],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
