{
    "name": "Raiden Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Dashboard page for the Raiden stateful-CLI RL environment benchmark",
    "description": """
Raiden Dashboard
================
A single-page dashboard for the Raiden reinforcement-learning environment:
20 Harbor-format agentic-coding tasks (10 aws s3 + 10 aws dynamodb) asking a model
to build the CLI surface from scratch, graded by a held-out suite of 1,577
E2E tests against simulated MinIO and DynamoDB Local backends. Available
as both a backend client action and a public portal page at /raiden.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/raiden_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        # Backend bundle (OWL component + SCSS) - loaded when an
        # authenticated user opens the "Raiden Dashboard" app/menu.
        "web.assets_backend": [
            "raiden_dashboard/static/src/scss/raiden_showcase.scss",
            "raiden_dashboard/static/src/components/showcase/showcase.js",
            "raiden_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Public /raiden page assets are NOT bundled into
        # web.assets_frontend on purpose - the portal template serves
        # them as bare <link>/<script> tags so Bootstrap and other
        # portal chrome do not override the dark editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
