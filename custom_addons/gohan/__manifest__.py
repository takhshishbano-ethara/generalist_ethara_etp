{
    "name": "Gohan PRD Pipeline",
    "version": "19.0.2.5.0",
    "category": "Productivity",
    "summary": "Operator UI for the Gohan functional-PRD scraping pipeline",
    "description": (
        "Gohan custom module for Ethara ETP. Trigger Gohan pipeline runs from "
        "Odoo; webhook callback from AWS Lambda backend. Automated website "
        "analysis and PRD generation using AWS Bedrock LLM with deterministic "
        "scoring.\n\n"
        "v19.0.2.0.0: RabbitMQ removed. Batch fan-out now uses direct async "
        "lambda:Invoke (InvocationType=Event). Capacity scales with Lambda "
        "ReservedConcurrentExecutions (default 250).\n\n"
        "v19.0.2.1.0: PRD is the deliverable — fail only on nothing-extracted "
        "or PRD-gen failure (not missing screenshots). Skip re-extraction when "
        "a PRD prompt already exists. New 'discarded' terminal state + Discard "
        "button. Robust self-recovering thread pool. Full transparency: raw "
        "Lambda callback + LLM trace per job. Watchdog 'started' ping.\n\n"
        "v19.0.2.4.0: Spec compliance overlay. API Gateway HTTP path coexists "
        "with direct boto3 invoke (action_run_pipeline + /gohan/webhook with "
        "HMAC-SHA256 verification). Spec-mandated sysparams (lambda_api_url, "
        "lambda_api_key, hmac_secret) exposed in Settings. gohan.category "
        "field 'technical_key' renamed to 'code' per spec; xmlids renamed "
        "'category_*' to 'cat_*' (migration auto-applies). New 'reconcile "
        "orphaned runs' cron (10 min) + 12 spec-mandated fields on gohan.job "
        "(notes, lambda_request_id, s3_artifact_prefix, score_max, qc counts, "
        "word_count, deliverable counts, ready_for_submission).\n\n"
        "v19.0.2.5.0: Extraction review gate. Interactive single jobs now "
        "park in a new 'extracted' state instead of auto-generating the PRD; "
        "the tasker curates the captured screenshots and SVG icons on the "
        "gohan.job.asset child model (delete unwanted rows, upload extra "
        "SVGs) in the Extraction Review tab, then clicks Generate PRD. Only "
        "ticked assets feed Bedrock; uploaded files are pushed to S3 first. "
        "Batch runs (via_batch) skip the gate and auto-generate as before."
    ),
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "base_setup", "web", "mail", "bus", "etp_user_roles"],
    "external_dependencies": {"python": ["requests", "markdown"]},
    "data": [
        "security/gohan_security.xml",
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "data/category_seed.xml",
        "data/cron.xml",
        "views/res_config_settings_views.xml",
        "views/gohan_job_views.xml",
        "views/gohan_category_views.xml",
        "views/menuitems.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "gohan/static/src/js/gohan_bus.js",
            "gohan/static/src/js/gohan_list.js",
            "gohan/static/src/js/gohan_json_pretty.js",
            "gohan/static/src/xml/gohan_list.xml",
            "gohan/static/src/xml/gohan_json_pretty.xml",
            "gohan/static/src/scss/gohan_statusbar.scss",
        ],
    },
    "installable": True,
    "application": True,
}
