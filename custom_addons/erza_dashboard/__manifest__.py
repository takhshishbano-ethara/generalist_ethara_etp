{
    "name": "Erza Dashboard",
    "version": "19.0.1.1.0",
    "category": "Productivity",
    "summary": "Dashboard for Erza - an Agent-Skills efficacy benchmark & RL environment",
    "description": """
Erza Dashboard
==============

Erza measures Agent-Skills efficacy via PAIRED evaluation: the same task is
run twice on the same model / harness / container - once with the curated
Skill injected, once without - and the reported result is the paired
difference (Δ). Efficacy is measured, never assumed; zero and negative-Δ
tasks are kept and labelled.

This dashboard is seeded from the first published Erza sample bundle
(natural-science interlaboratory-metrology task, ERZA-RB1 robust consensus;
claude-opus-4-8; 3 paired trials per arm) sourced from the erza-samples
repository, and provides:

* Persistent ORM models for task bundles, models, and paired runs (with Δ).
* Backend list / kanban / form / graph / pivot views.
* An OWL client action giving an executive overview.
* A public portal page at /erza-samples with an interactive task-bundle viewer.
* A JSON API exposing summary, tasks, and runs.

Data source:
* https://github.com/Ethara-Ai/erza-samples (MIT)
""",
    "author": "Ethara.AI",
    "website": "https://www.ethara.ai",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
        "views/erza_task_views.xml",
        "views/erza_run_views.xml",
        "views/erza_model_views.xml",
        "views/erza_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "erza_dashboard/static/src/scss/erza_showcase.scss",
            "erza_dashboard/static/src/components/showcase/showcase.js",
            "erza_dashboard/static/src/components/showcase/showcase.xml",
        ],
    },
    "post_init_hook": "_erza_post_init",
    "installable": True,
    "application": True,
    "auto_install": False,
}
