{
    "name": "Valkyrie Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Showcase page for the Valkyrie security vulnerability remediation benchmark",
    "description": """
Valkyrie Dashboard
==================
A single-page showcase displaying the Valkyrie security vulnerability
remediation benchmark — autonomous patch generation across 20 instances
spanning 7 CWE classes using reinforcement learning agents. Available as
both a backend client action and a public portal page at /valkyrie.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "portal", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/valkyrie_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "valkyrie_dashboard/static/src/scss/valkyrie_showcase.scss",
            "valkyrie_dashboard/static/src/components/showcase/showcase.js",
            "valkyrie_dashboard/static/src/components/showcase/showcase.xml",
        ],
        "web.assets_frontend": [
            "valkyrie_dashboard/static/src/portal/css/valkyrie_portal.css",
            "valkyrie_dashboard/static/src/portal/js/valkyrie_portal.js",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
