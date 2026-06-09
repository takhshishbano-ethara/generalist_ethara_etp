{
    "name": "ETP Assessment Extension",
    "version": "19.0.1.0.0",
    "category": "Human Resources",
    "summary": "REST API for the ETP Assessment module.",
    "description": """
        REST API layer for the ETP Assessment module, exposing the question
        bank, assessments, candidate assignments, responses, dashboard KPIs,
        and the public candidate portal flow over JSON endpoints.

        Real data (read live from etp.assessment.* models):
          - Dashboard KPIs (assessments, questions, candidates, responses)
          - Question / category / dimension CRUD
          - Assessment lifecycle (draft -> in_progress -> done / cancelled)
          - Candidate assignment + CSV bulk import
          - Per-dimension response analytics

        Candidate portal (token-auth, no login required):
          - Resolve assignment by access_token
          - Begin assessment (starts timer)
          - Fetch current question + dimensions
          - Submit response with selected option per dimension
          - Submit violation event (auto-locks the assignment)

        All responses follow the api_auth_gateway return_Response envelope
        and are guarded by @validate_token, matching aurora_extension /
        crowley_extension / employee_extension.
    """,
    "author": "Ethara",
    "website": "https://www.ethara.com",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "hr",
        "mail",
        "etp_assessment",
        "api_auth_gateway",
    ],
    "data": [
        "security/ir.model.access.csv",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
