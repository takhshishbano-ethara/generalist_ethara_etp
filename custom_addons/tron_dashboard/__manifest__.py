{
    "name": "Tron Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Tron Dashboard (placeholder - content TBD)",
    "description": """
Tron Dashboard
==============
Placeholder dashboard scaffold for the Tron project. Structure and design
cloned from Kaiju Dashboard; content is stubbed with TODO markers pending
final copy and data. Available as both a backend client action and a public
portal page at /tron.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/tron_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "tron_dashboard/static/src/scss/tron_showcase.scss",
            "tron_dashboard/static/src/components/showcase/showcase.js",
            "tron_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Portal CSS/JS served as bare <link>/<script> in the template
        # to avoid Bootstrap/portal chrome interference with the
        # dark editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
