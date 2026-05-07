{
    "name": "Drengr Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Showcase page for the OpenAgentSafety / Drengr benchmark",
    "description": """
Drengr Dashboard
================
A single-page showcase displaying the OpenAgentSafety benchmark —
evaluating AI agent safety in realistic, high-risk workplace simulations
across 361 scenarios. Public portal page at /drengr.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "portal", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/drengr_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {},
    "installable": True,
    "application": True,
    "auto_install": False,
}
