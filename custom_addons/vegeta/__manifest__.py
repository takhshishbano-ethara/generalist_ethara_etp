{
    "name": "Vegeta",
    "version": "19.0.2.5.0",
    "category": "Tools",
    "summary": "Vegeta — Automated PRD Generation Pipeline (worker-pool, max-pack)",
    "description": (
        "Vegeta custom module for Ethara ETP. Automated website analysis and "
        "PRD generation using AWS Bedrock LLM with deterministic scoring.\n\n"
        "v19.0.2.0.0: RabbitMQ removed. Batch fan-out now uses direct async "
        "lambda:Invoke (InvocationType=Event). Capacity scales with Lambda "
        "ReservedConcurrentExecutions (default 250).\n\n"
        "v19.0.2.1.0: PRD is the deliverable — fail only on nothing-extracted "
        "or PRD-gen failure (not missing screenshots). Skip re-extraction when "
        "a PRD prompt already exists. New 'discarded' terminal state + Discard "
        "button. Robust self-recovering thread pool. Full transparency: raw "
        "Lambda callback + LLM trace per job. Watchdog 'started' ping.\n\n"
        "v19.0.2.4.0: PRD generation runs in a dedicated Kubernetes Job per "
        "job (ported from the aurora addon) so the work survives Odoo "
        "worker/pod recycling and concurrent jobs scale across pods. "
        "Cron-driven dispatch + reconcile, with an in-process fallback for "
        "local single-process development.\n\n"
        "v19.0.2.5.0: Replaces K8s-Job-per-task with a fixed-replica "
        "Deployment of long-lived PRD worker pods. Each pod claims up to "
        "VEGETA_WORKER_CONCURRENCY (default 100) jobs via SELECT ... FOR "
        "UPDATE SKIP LOCKED. Eliminates per-task Odoo cold-start (~30-60s) "
        "and the 'huge pod count' problem at 500+ tasks/day. Odoo dispatch "
        "cron is now a no-op in production; reconcile cron recovers jobs "
        "with stale heartbeats by clearing job_name for re-claim."
    ),
    "author": "Ethara",
    "depends": ["base", "base_setup", "web", "mail", "bus", "etp_user_roles"],
    "post_load": "post_load",
    "data": [
        "security/vegeta_security.xml",
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "data/category_seed.xml",
        "data/cron.xml",
        "views/res_config_settings_views.xml",
        "views/vegeta_job_views.xml",
        "views/menuitems.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "vegeta/static/src/js/vegeta_bus.js",
            "vegeta/static/src/js/vegeta_list.js",
            "vegeta/static/src/xml/vegeta_list.xml",
            "vegeta/static/src/scss/vegeta_statusbar.scss",
        ],
    },
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
