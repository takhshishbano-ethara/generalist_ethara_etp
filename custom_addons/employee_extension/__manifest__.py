{
    "name": "Employee Extension",
    "version": "19.0.1.0.0",
    "category": "Human Resources",
    "summary": "Employee offboarding and allocation request management",
    "description": """
Employee Extension Module
========================
This module extends the hr.employee functionality with:
- Offboarding workflow (active -> offboarding -> offboarded)
- Employee allocation request system
- Role-based filtering for allocation requests
- REST APIs for employee CRUD and offboarding
- REST APIs for allocation requests and approvals
    """,
    "author": "ERP Team",
    "website": "",
    "license": "LGPL-3",
    "depends": [
        "base",
        "hr",
        "project",
        "api_auth_gateway",
        "mail",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/onboarding_email_template.xml",
        "views/hr_employee_views.xml",
        "views/allocation_request_views.xml",
        "views/assignment_history_views.xml",
        "views/menu_views.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
