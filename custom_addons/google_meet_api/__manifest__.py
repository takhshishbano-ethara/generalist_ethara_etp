{
    "name": "Google Meet REST API",
    "version": "19.0.1.1.0",
    "category": "Productivity",
    "summary": "REST API endpoints for the Google Meet + Calendar integration",
    "description": """
Google Meet REST API
====================
Exposes /api/v1/google_meet/... endpoints backed by the
odoo_google_meet_integration module. Uses api_auth_gateway for
access-token authentication and the platform-wide JSON response
envelope (return_Response).

Endpoints
---------
* GET    /api/v1/google_meet/config
* POST   /api/v1/google_meet/config
* POST   /api/v1/google_meet/authenticate
* POST   /api/v1/google_meet/refresh_token
* GET    /api/v1/google_meet/events
* POST   /api/v1/google_meet/events
* GET    /api/v1/google_meet/events/<id>
* PUT    /api/v1/google_meet/events/<id>
* DELETE /api/v1/google_meet/events/<id>
""",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "hr",
        "hr_recruitment",
        "calendar",
        "api_auth_gateway",
        "odoo_google_meet_integration",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/api_endpoint_data.xml",
        "data/mail_template.xml",
        "wizard/schedule_wizard_views.xml",
        "wizard/reschedule_wizard_views.xml",
        "wizard/cancel_wizard_views.xml",
        "views/calendar_event_views.xml",
    ],
    "installable": True,
    "auto_install": False,
    "application": False,
}
