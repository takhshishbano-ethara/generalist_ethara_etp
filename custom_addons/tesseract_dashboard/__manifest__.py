{
    "name": "Tesseract Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Showcase dashboard for the Tesseract Multimodal SWE-Bench dataset",
    "description": """
Tesseract Dashboard
===================
A single-page showcase displaying the Tesseract Multimodal SWE-Bench
benchmark by ETHARA AI . Available as both a backend client action
and a public portal page at /tesseract.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web"],
    "data": [
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
        "views/tesseract_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        # Backend bundle (OWL component + SCSS) — loaded when an
        # authenticated user opens the "Tesseract Showcase" app/menu.
        "web.assets_backend": [
            "tesseract_dashboard/static/src/scss/tesseract_dashboard.scss",
            "tesseract_dashboard/static/src/components/showcase/showcase.js",
            "tesseract_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Public /tesseract page assets are NOT bundled into
        # web.assets_frontend on purpose — the portal template serves
        # them as bare <link>/<script> tags so Bootstrap and other
        # portal chrome do not override the dark editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
