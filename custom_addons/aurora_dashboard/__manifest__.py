{
    "name": "Aurora Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Dashboard page for the Aurora RL environment benchmark",
    "description": """
Aurora Dashboard
================
A single-page dashboard displaying the Aurora multi-language long-horizon
software evaluation benchmark — sequences of 1 to 100+ consecutive PRs
across 8 programming languages using the Milo-Bench framework. Available
as both a backend client action and a public portal page at /aurora.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "portal", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/aurora_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "aurora_dashboard/static/src/scss/aurora_showcase.scss",
            "aurora_dashboard/static/src/components/showcase/showcase.js",
            "aurora_dashboard/static/src/components/showcase/showcase.xml",
        ],
        "web.assets_frontend": [
            "aurora_dashboard/static/src/portal/css/aurora_portal.css",
            "aurora_dashboard/static/src/portal/js/aurora_portal.js",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
