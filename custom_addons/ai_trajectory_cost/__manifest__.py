{
    "name": "AI Trajectory Cost Tracking",
    "version": "19.0.1.0.0",
    "summary": "Store per-trajectory LLM token usage and cost, broken down by model",
    "category": "Accounting/Cost Management",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/ir.model.access.csv",
        "views/trajectory_cost_views.xml",
        "views/menus.xml",
    ],
    "application": True,
    "installable": True,
    "auto_install": False,
}
