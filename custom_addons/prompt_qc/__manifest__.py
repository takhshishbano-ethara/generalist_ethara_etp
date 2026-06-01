{
    'name': 'Prompt QC',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'LLM-as-a-judge QC: stream a judge verdict for a prompt via SSE',
    'description': """
        The Prompt QC project.

        A QC run takes:
        - a user prompt (the subject being evaluated),
        - an uploaded .md system prompt (the judge's instructions), and
        - an optional uploaded .json rubric (the criteria the judge applies).

        On "Start QC" the backend makes one AWS Bedrock converse-stream call where the
        uploaded .md is the system prompt, the user prompt is the user message, and the
        rubric .json (when uploaded) is sent as a second system block. The response is
        streamed to the page via Server-Sent Events (fire-forward, kensei2 convention)
        and rendered incrementally. The full result is persisted only after the stream
        completes; a partial/aborted stream is saved as 'failed', never 'done'.

        The rubric is a passthrough: it is forwarded to the judge as text, not parsed into
        scores. Structured scoring remains a possible future extension.
    """,
    'author': 'Ethara',
    'website': '',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'depends': [
        'base',
        'web',
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
