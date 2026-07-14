{
    "name": "Milo-Bench Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Dashboard for the Milo-Bench long-horizon software-engineering benchmark",
    "description": """
Milo-Bench Dashboard
====================

Mirrors the public Milo-Bench samples dataset (30 tasks x 3 models x 3 runs =
270 graded runs) with:

* Persistent ORM models for tasks, models, and runs.
* Backend list / kanban / form / graph / pivot views.
* An OWL client action giving an executive overview.
* A public portal page at /milobench-samples with an interactive tasks viewer.
* A JSON API exposing summary, tasks, and runs.

Data sources:
* https://github.com/EtharaOrion/milo-bench-samples (MIT)
* https://huggingface.co/datasets/ethara/milo-bench-samples (CC-BY-NC-ND-4.0)
""",
    "author": "Ethara.AI",
    "website": "https://www.ethara.ai",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
        "views/milobench_task_views.xml",
        "views/milobench_run_views.xml",
        "views/milobench_model_views.xml",
        "views/milobench_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "milobench_dashboard/static/src/scss/milobench_showcase.scss",
            "milobench_dashboard/static/src/components/showcase/showcase.js",
            "milobench_dashboard/static/src/components/showcase/showcase.xml",
        ],
    },
    "post_init_hook": "_milobench_post_init",
    "installable": True,
    "application": True,
    "auto_install": False,
}
