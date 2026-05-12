{
    'name': 'ARC QC',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Automated QC validation for ARC-AGI-3 trajectory deliveries',
    'description': """
        Deterministic (no-LLM) quality control engine for ARC-AGI-3 game
        trajectory data produced by the arc-explainer eval harness.

        Validates:
        - Package structure (4 canonical models, 3 runs each)
        - runs.jsonl schema compliance (25 required fields, 7 invariants)
        - steps.jsonl schema compliance (23 required fields, sequence checks)
        - Cross-run consistency (step counts, run_id linkage)
        - Content safety (~40 regex patterns for leakage, injection, PII)
        - Smell tests (cookie-cutter CoT, synthetic timestamps, etc.)

        Produces SHIP / CONDITIONAL_SHIP / BLOCK verdicts per QC spec.
    """,
    'author': 'Ethara',
    'website': '',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'depends': [
        'base',
        'mail',
        'arc_eval_launcher',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'report/arc_qc_game_result_report.xml',
        'report/arc_qc_game_result_report_template.xml',
        'views/arc_qc_session_views.xml',
        'views/arc_qc_game_result_views.xml',
        'views/arc_qc_finding_views.xml',
        'views/menu_items.xml',
    ],
    'demo': [],
    'assets': {
        'web.assets_backend': [
            'arc_qc/static/src/dashboard/arc_qc_dashboard.js',
            'arc_qc/static/src/dashboard/arc_qc_dashboard.xml',
            'arc_qc/static/src/dashboard/arc_qc_dashboard.scss',
        ],
    },
}
