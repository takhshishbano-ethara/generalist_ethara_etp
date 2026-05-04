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
    "depends": ["base", "web", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/aurora_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        # Backend bundle (OWL component + SCSS) — loaded when an
        # authenticated user opens the "Aurora Showcase" app/menu.
        "web.assets_backend": [
            "aurora_dashboard/static/src/scss/aurora_showcase.scss",
            "aurora_dashboard/static/src/components/showcase/showcase.js",
            "aurora_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Public /aurora page assets are NOT bundled into
        # web.assets_frontend on purpose — the portal template serves
        # them as bare <link>/<script> tags so Bootstrap and other
        # portal chrome do not override the dark editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
