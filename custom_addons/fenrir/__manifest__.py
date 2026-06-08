{
    "name": "Fenrir",
    "version": "19.0.1.14.0",
    "category": "Tools",
    "summary": "Fenrir — Freelancer task & seller negotiation tracker",
    "description": """
        Fenrir custom module for Ethara ETP.
        Tracks freelance project tasks (category, title, overview, scope,
        rubrics, instruction docs, status, buyer & pricing) alongside the
        full per-seller negotiation history (initial ask, negotiated offer,
        conversation log, accepted offer, final payment, delivery state,
        deliverables and automated checks) mirroring the team's tracking
        spreadsheet.
    """,
    "author": "Ethara",
    "depends": ["base", "mail"],
    "external_dependencies": {
        "python": ["google-api-python-client", "google-auth"],
    },
    "data": [
        "security/fenrir_security.xml",
        "security/ir.model.access.csv",
        "views/fenrir_category_views.xml",
        "views/fenrir_environment_runtime_views.xml",
        "views/fenrir_rubric_import_wizard_views.xml",
        "views/fenrir_rubric_score_import_wizard_views.xml",
        "views/fenrir_task_views.xml",
        "views/fenrir_seller_offer_views.xml",
        "views/fenrir_drive_config_views.xml",
        "views/menuitems.xml",
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
