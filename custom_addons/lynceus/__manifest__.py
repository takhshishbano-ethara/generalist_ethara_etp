{
    "name": "Lynceus - Prompt-to-Tasker Allocation",
    "version": "19.0.12.0.4",
    "category": "Operations/Quality",
    "summary": (
        "Generate AI-image adversarial prompts via Gemini 3.5 Flash on GCP Vertex AI "
        "(batched, one LLM call yields N prompts as N records), distribute them fairly "
        "to active taskers, capture outcomes, and recycle untouched prompts after 24h "
        "inactivity. No prompt is ever duplicated, double-worked, lost, or regenerated."
    ),
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "mail"],
    "external_dependencies": {
        "python": ["cryptography", "requests"],
    },
    "data": [
        "security/lynceus_security.xml",
        "security/ir.model.access.csv",
        "security/lynceus_record_rules.xml",
        "data/lynceus_sequence.xml",
        "data/ir_cron.xml",
        "wizards/generate_batch_wizard_views.xml",
        "wizards/import_active_taskers_wizard_views.xml",
        "views/lynceus_prompt_views.xml",
        "views/lynceus_batch_views.xml",
        "views/lynceus_tasker_queue_views.xml",
        "views/lynceus_dashboard_views.xml",
        "views/res_config_settings_views.xml",
        "views/lynceus_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js",
            "lynceus/static/src/scss/lynceus.scss",
            "lynceus/static/src/dashboard/dashboard.scss",
            "lynceus/static/src/dashboard/dashboard.js",
            "lynceus/static/src/dashboard/dashboard.xml",
        ],
    },
    "application": True,
    "installable": True,
}
