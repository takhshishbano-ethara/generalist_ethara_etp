{
    "name": "Jaeger - SWE Task Data Collection Pipeline",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "SWE task data collection: GitHub PR scraping → raw dataset (Phase 1)",
    "description": """
Jaeger - SWE Task Data Collection Pipeline
===========================================

A production-grade pipeline for collecting software engineering task data:

* **Phase 1**: GitHub PR scraping and raw dataset creation
* **Phase 2**: Docker image building, 3-run test execution, dataset finalization
* **Phase 3**: AI trajectory generation on EKS, pass@k evaluation, Meta delivery export

Supports 10,000+ repos with RabbitMQ-based durable job processing.
    """,
    "author": "Ethara AI",
    "website": "https://www.ethara.ai",
    "depends": ["base", "mail", "web"],
    "external_dependencies": {
        "python": ["github", "unidiff", "boto3"],
    },
    "data": [
        "security/jaeger_security.xml",
        "security/ir.model.access.csv",
        "data/jaeger_data.xml",
        "data/cron.xml",
        "views/jaeger_instance_views.xml",
        "views/jaeger_run_views.xml",
        "views/jaeger_repository_views.xml",
        "views/res_config_settings_views.xml",
        "views/jaeger_menus.xml",
        "views/import_repos_wizard_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "jaeger/static/src/scss/jaeger_form.scss",
            "jaeger/static/src/components/instance_progress/instance_progress.js",
            "jaeger/static/src/components/instance_progress/instance_progress.xml",
            "jaeger/static/src/components/instance_progress/instance_progress.scss",
            "jaeger/static/src/components/run_dashboard/run_dashboard.js",
            "jaeger/static/src/components/run_dashboard/run_dashboard.xml",
            "jaeger/static/src/components/run_dashboard/run_dashboard.scss",
            "jaeger/static/src/components/auto_refresh/auto_refresh.js",
            "jaeger/static/src/components/auto_refresh/auto_refresh.xml",
            "jaeger/static/src/components/log_viewer/log_viewer.js",
            "jaeger/static/src/components/log_viewer/log_viewer.xml",
            "jaeger/static/src/components/log_viewer/log_viewer.scss",
        ],
    },
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
