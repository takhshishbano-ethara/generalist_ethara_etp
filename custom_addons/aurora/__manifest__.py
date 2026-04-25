{
    "name": "Aurora Pipeline",
    "version": "19.0.1.0.0",
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
        "python": ["github", "packaging", "requests", "unidiff", "tqdm", "dotenv", "boto3", "cryptography", "kubernetes", "openpyxl"],
    },
    "application": True,
    "data": [
        "security/aurora_security.xml",
        "security/ir.model.access.csv",
        "data/data.xml",
        "views/pipeline_views.xml",
        "views/token_views.xml",
        "views/aurora_menus.xml",
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "aurora/static/src/scss/aurora_form.scss",
            "aurora/static/src/components/auto_refresh/auto_refresh.js",
            "aurora/static/src/components/auto_refresh/auto_refresh.xml",
        ],
    },
    "installable": True,
    "auto_install": False,
    "uninstall_hook": "uninstall_hook",
}
