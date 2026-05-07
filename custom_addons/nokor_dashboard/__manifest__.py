{
    "name": "Nokor Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Dashboard page for the Nokor AI assistant benchmark",
    "description": """
Nokor Dashboard
===============
A single-page dashboard displaying the Nokor multi-modal reasoning and
tool-use evaluation benchmark — evaluating AI assistants across PDF, Web,
Image, Video, and Reasoning modalities using the GAIA framework. Available
as both a backend client action and a public portal page at /nokor.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/nokor_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        # Backend bundle (OWL component + SCSS) — loaded when an
        # authenticated user opens the "Nokor Dashboard" app/menu.
        "web.assets_backend": [
            "nokor_dashboard/static/src/scss/nokor_showcase.scss",
            "nokor_dashboard/static/src/components/showcase/showcase.js",
            "nokor_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Public /nokor page assets are NOT bundled into
        # web.assets_frontend on purpose — the portal template serves
        # them as bare <link>/<script> tags so Bootstrap and other
        # portal chrome do not override the dark editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
