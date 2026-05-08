{
    "name": "Janus Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Showcase page for the Janus benchmark dataset",
    "description": """
Janus Dashboard
==================
A single-page showcase displaying the Janus AI benchmark dataset.
Available as both a backend client action and a public portal page at /Janus.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "portal", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/akatsuki_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "akatsuki_dashboard/static/src/scss/akatsuki_showcase.scss",
            "akatsuki_dashboard/static/src/components/showcase/showcase.js",
            "akatsuki_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Portal CSS/JS served as bare <link>/<script> in the template
        # to avoid Bootstrap/portal chrome interference with the
        # editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
