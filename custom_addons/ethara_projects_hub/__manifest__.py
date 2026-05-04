{
    "name": "Ethara Projects Hub",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Central hub with hyperlinks to all Ethara project dashboards",
    "description": """
Ethara Projects Hub
===================
An independent module that displays a configurable set of project
dashboard cards (Kaiju, Kraken, Aurora, Valkyrie, Tesseract).
Each card links to the corresponding project page on
projects.ethara.ai. Available as both a backend client action
and a public portal page at /projects.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "portal", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/projects_hub_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ethara_projects_hub/static/src/scss/projects_hub.scss",
            "ethara_projects_hub/static/src/components/hub/hub.js",
            "ethara_projects_hub/static/src/components/hub/hub.xml",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
