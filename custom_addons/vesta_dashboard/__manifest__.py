{
    "name": "Vesta Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Showcase page for the Vesta RL safety environments",
    "description": """
Vesta Dashboard
===============
A single-page showcase displaying Vesta — RL environments for
training model safety under adversarial social pressure.
Public portal page at /vesta.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "portal", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/vesta_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {},
    "installable": True,
    "application": True,
    "auto_install": False,
}
