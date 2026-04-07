{
    "name": "Talos",
    "version": "19.0.4.0.0",
    "category": "Tools",
    "summary": "Talos — LLM task management with sandbox environments",
    "description": """
        Talos custom module for Ethara ETP.
        Local Docker Compose sandbox execution and K8s-native sandbox
        deployments for OpenClaw task environments.
    """,
    "author": "Ethara",
    "depends": ["base", "web", "hr"],
    "data": [
        "security/talos_security.xml",
        "security/ir.model.access.csv",
        "data/cron.xml",
        "views/res_config_settings_views.xml",
        "views/domain_views.xml",
        "views/talos_views.xml",
        "views/menuitems.xml",
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
