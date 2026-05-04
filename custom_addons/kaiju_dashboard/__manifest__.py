{
    "name": "Kaiju Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Showcase page for the Kaiju/Commit0 dataset",
    "description": """
Kaiju Dashboard
===============
A single-page showcase displaying the Kaiju/Commit0 AI coding dataset —
from-scratch Python library generation across 300 instances using the
Milo-Bench methodology. Available as both a backend client action and a
public portal page at /kaiju.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "portal", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/kaiju_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "kaiju_dashboard/static/src/scss/kaiju_showcase.scss",
            "kaiju_dashboard/static/src/components/showcase/showcase.js",
            "kaiju_dashboard/static/src/components/showcase/showcase.xml",
        ],
        "web.assets_frontend": [
            "kaiju_dashboard/static/src/portal/css/kaiju_portal.css",
            "kaiju_dashboard/static/src/portal/js/kaiju_portal.js",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
