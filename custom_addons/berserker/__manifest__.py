{
    "name": "Berserker",
    "version": "19.0.1.0.0",
    "category": "Uncategorized",
    "summary": "Berserker custom module",
    "description": """
        Berserker custom module for Ethara ETP.
    """,
    "author": "Ethara",
    "depends": ["base", "base_setup", "web", "hr", "bus"],
    "external_dependencies": {"python": ["pika"]},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/data.xml",
        "wizard/mass_assign_views.xml",
        "views/res_config_settings_views.xml",
        "views/berserker_views.xml",
        "views/usage_log_views.xml",
        "views/menuitems.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "berserker/static/src/services/eval_notification_service.js",
            "berserker/static/src/views/berserker_form.scss",
            "berserker/static/src/views/fields/markdown_latex_field/markdown_latex_field.js",
            "berserker/static/src/views/fields/markdown_latex_field/markdown_latex_field.xml",
            "berserker/static/src/views/fields/markdown_latex_field/markdown_latex_field.scss",
            "berserker/static/src/views/fields/ranking_field/ranking_field.js",
            "berserker/static/src/views/fields/ranking_field/ranking_field.xml",
            "berserker/static/src/views/fields/ranking_field/ranking_field.scss",
        ],
    },
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
