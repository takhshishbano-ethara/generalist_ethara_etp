{
    "name": "Employee Role Import",
    "version": "19.0.1.0.0",
    "category": "Human Resources",
    "summary": "Bulk-import employees from CSV and assign an ETP role in one step.",
    "description": """
Employee Role Import
====================
Provides a wizard that imports employees from a CSV file with the columns
``name``, ``employee_id`` and ``email`` and assigns an ETP role
(TPM / PL / QL / QR / DM / CTO / HR Admin / IT Admin / Tasker) to every
employee in the batch.

The role is picked once on the wizard - independently from the CSV - so the
same source file can be used to onboard people into any role.
    """,
    "author": "ERP Team",
    "website": "",
    "license": "LGPL-3",
    "depends": [
        "base",
        "hr",
        "etp_user_roles",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/hr_employee_views.xml",
        "views/employee_role_import_views.xml",
        "views/menu.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "employee_role_import/static/src/css/wizard.css",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
