{
    'name': 'Prompt QC',
    'version': '19.0.2.0.0',
    'category': 'Productivity',
    'summary': 'LLM-as-a-judge QC: background judge runs with live bus streaming',
    'description': """
        The Prompt QC project.

        A QC run takes:
        - a user prompt (the subject being evaluated),
        - an uploaded .md system prompt (the judge's instructions), and
        - an optional uploaded .json rubric (the criteria the judge applies).

        On "Start QC" (and on "Bulk Import") the run is queued and processed by a background
        worker pool — the AWS Bedrock call never runs inside the HTTP request, so a flood of
        concurrent clicks never ties up the web workers. Concurrency is capped at the pool size
        and throttles (HTTP 429 / 5xx) are retried with exponential backoff + jitter. While a run
        executes, the judge's tokens stream live to the run owner over the bus (bus.bus); the full
        result is persisted only after completion (a partial/aborted run is 'failed', never 'done').

        The rubric is a passthrough: it is forwarded to the judge as text, not parsed into
        scores. Structured scoring remains a possible future extension.

        Scalability is documented in docs/SCALABILITY_AND_DEPLOYMENT.md (deployment settings the
        ops side must apply: prefork workers, the websocket/gevent port, DB connection sizing,
        and the PROMPT_QC_POOL_SIZE env var).
    """,
    'author': 'Ethara',
    'website': '',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'depends': [
        'base',
        'web',
        'bus',
    ],
    'external_dependencies': {
        'python': ['httpx'],
    },
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/ir_cron_data.xml',
        'views/prompt_qc_run_views.xml',
        'views/res_config_settings_views.xml',
        'views/menu.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'prompt_qc/static/src/prompt_qc/prompt_qc_stream_service.js',
            'prompt_qc/static/src/prompt_qc/prompt_qc_field.js',
            'prompt_qc/static/src/prompt_qc/prompt_qc_field.xml',
            'prompt_qc/static/src/prompt_qc/prompt_qc_markdown_field.js',
            'prompt_qc/static/src/prompt_qc/prompt_qc_markdown_field.xml',
            'prompt_qc/static/src/prompt_qc/prompt_qc_list.js',
            'prompt_qc/static/src/prompt_qc/prompt_qc_list.xml',
            'prompt_qc/static/src/prompt_qc/bulk_import_dialog.js',
            'prompt_qc/static/src/prompt_qc/bulk_import_dialog.xml',
            'prompt_qc/static/src/prompt_qc/prompt_qc.scss',
        ],
    },
}
