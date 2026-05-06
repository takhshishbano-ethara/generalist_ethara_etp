{
    "name": "Loki Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "ECG Record Viewer — JSON + Image split-panel dashboard",
    "description": """
Loki Dashboard
==============
A split-panel ECG record viewer displaying JSON data on one side and the
corresponding ECG image on the other. Includes record navigation to browse
through all available ECG records. Available as a public portal page at /loki.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "portal", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/loki_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
