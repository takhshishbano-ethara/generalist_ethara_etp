{
    "name": "Fenrir",
    "version": "19.0.1.23.0",
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
        "python": ["google-api-python-client", "google-auth", "boto3"],
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
    "assets": {
        "web.assets_backend": [
            "fenrir/static/src/views/fields/deliverable_files/deliverable_files.js",
            "fenrir/static/src/views/fields/deliverable_files/deliverable_files.xml",
            "fenrir/static/src/views/fields/deliverable_files/deliverable_files.scss",
            "fenrir/static/src/views/fields/deliverable_inline/deliverable_inline.js",
            "fenrir/static/src/views/fields/deliverable_inline/deliverable_inline.xml",
            "fenrir/static/src/views/fields/deliverable_inline/deliverable_inline.scss",
            "fenrir/static/src/views/widgets/attachment_uploader/attachment_uploader.js",
            "fenrir/static/src/views/widgets/attachment_uploader/attachment_uploader.xml",
            "fenrir/static/src/views/widgets/attachment_uploader/attachment_uploader.scss",
            "fenrir/static/src/views/fenrir_seller_stacked.scss",
            "fenrir/static/src/views/fenrir_conversation.scss",
        ],
    },
}
