{
    "name": "Aurora Pipeline",
    "version": "19.0.6.0.0",
    "category": "Productivity",
    "summary": "Aurora data-collection pipeline",
    "description": """
        Odoo front-end for the Aurora data-collection pipeline.
        Fetches PRs, discovers version tags, groups PRs by tag pairs,
        fetches related issues, and builds the final dataset – all from
        the Odoo UI with background execution.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "mail", "bus"],
    "external_dependencies": {
        "python": ["github", "packaging", "requests", "unidiff", "tqdm", "dotenv", "boto3", "cryptography", "kubernetes", "openpyxl","docker", "git", "dataclasses_json", "yaml"],
    },
    "application": True,
    "data": [
        "security/aurora_security.xml",
        "security/ir.model.access.csv",
        "data/data.xml",
        "views/pipeline_views.xml",
        "views/evaluation_views.xml",
        "views/evaluation_instance_views.xml",
        "views/harness_staging_views.xml",
        "views/registry_wizard_views.xml",
        "views/import_dataset_wizard_views.xml",
        "views/token_views.xml",
        "views/discovery_views.xml",
        "views/discovery_wizard_views.xml",
        "views/aurora_menus.xml",
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "aurora/static/src/scss/aurora_form.scss",
            "aurora/static/src/aurora_bus_service.js",
            "aurora/static/src/components/auto_refresh/auto_refresh.js",
            "aurora/static/src/components/auto_refresh/auto_refresh.xml",
            "aurora/static/src/components/dashboard/dashboard.js",
            "aurora/static/src/components/dashboard/dashboard.xml",
            "aurora/static/src/components/statusbar/statusbar.js",
            "aurora/static/src/components/statusbar/statusbar.xml",
        ],
    },
    "installable": True,
    "auto_install": False,
    "uninstall_hook": "uninstall_hook",
}
