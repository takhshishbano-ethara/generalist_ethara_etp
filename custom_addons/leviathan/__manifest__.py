{
    "name": "Leviathan",
    "version": "19.0.6.0.0",
    "category": "Tools",
    "summary": "Leviathan — Automated PRD Generation Pipeline (async-Lambda, 250-concurrent)",
    "description": (
        "Leviathan custom module for Ethara ETP. Automated website analysis and "
        "PRD generation using AWS Bedrock LLM with deterministic scoring.\n\n"
        "v19.0.2.0.0: RabbitMQ removed. Batch fan-out now uses direct async "
        "lambda:Invoke (InvocationType=Event). Capacity scales with Lambda "
        "ReservedConcurrentExecutions (default 250).\n\n"
        "v19.0.2.1.0: PRD is the deliverable — fail only on nothing-extracted "
        "or PRD-gen failure (not missing screenshots). Skip re-extraction when "
        "a PRD prompt already exists. New 'discarded' terminal state + Discard "
        "button. Robust self-recovering thread pool. Full transparency: raw "
        "Lambda callback + LLM trace per job. Watchdog 'started' ping.\n\n"
        "v19.0.5.0.0: durable PRD queue with FOR UPDATE SKIP LOCKED claim, "
        "prd_claim_count fence, two-mode dispatch (inprocess|worker).\n\n"
        "v19.0.6.0.0: per-job Logs tab + log handler (auto-scrapes [job=N] "
        "tags from anywhere in the addon), current_phase sub-step visibility, "
        "lambda_request_id capture for CloudWatch fetch, chatter posts at "
        "every pipeline boundary."
    ),
    "author": "Ethara",
    "depends": ["base", "base_setup", "web", "mail", "bus", "etp_user_roles"],
    "post_load": "post_load",
    "data": [
        "security/leviathan_security.xml",
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "data/category_seed.xml",
        "data/cron.xml",
        "views/res_config_settings_views.xml",
        "views/leviathan_job_views.xml",
        "views/menuitems.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "leviathan/static/src/js/leviathan_bus.js",
            "leviathan/static/src/js/leviathan_list.js",
            "leviathan/static/src/xml/leviathan_list.xml",
            "leviathan/static/src/scss/leviathan_statusbar.scss",
        ],
    },
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
