{
    "name": "Terra Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Dashboard page for the Terra RL environment for general AI assistants",
    "description": """
Terra Dashboard
===============
A single-page dashboard displaying the Terra multi-modal reasoning and
tool-use RL environment. Trains AI assistants across Web Browsing, File Reading,
Image, Video, and Reasoning categories using the GAIA methodology. Available
as both a backend client action and a public portal page at /terra.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "data/config_data.xml",
        "views/res_config_settings_views.xml",
        "views/nokor_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        # Backend bundle (OWL component + SCSS) — loaded when an
        # authenticated user opens the "Terra Dashboard" app/menu.
        "web.assets_backend": [
            "nokor_dashboard/static/src/scss/nokor_showcase.scss",
            "nokor_dashboard/static/src/components/showcase/showcase.js",
            "nokor_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Public /terra page assets are NOT bundled into
        # web.assets_frontend on purpose — the portal template serves
        # them as bare <link>/<script> tags so Bootstrap and other
        # portal chrome do not override the dark editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
