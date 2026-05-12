{
    'name': 'ARC Eval Launcher',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Launch ARC-Explainer eval runs from Odoo',
    'description': """
        Ultra-MVP integration with arc-explainer eval harness.

        Users can trigger eval runs from Odoo (selecting games, models, and run
        parameters) via a wizard that calls the arc-explainer HTTP API
        (POST /api/eval/start). A lightweight Session record is created for
        audit/tracking. No live monitoring, no step detail - V1 is trigger-only.

        Games and Models are cached locally (auto-refreshed via cron from
        arc-explainer) and selected via M2M in the wizard.
    """,
    'author': 'Ethara',
    'website': '',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'depends': [
        'base',
        'web',
        'mail',
    ],
    'external_dependencies': {
        'python': ['requests'],
    },
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_config_parameter_data.xml',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/arc_eval_game_views.xml',
        'views/arc_eval_model_views.xml',
        'views/arc_eval_session_views.xml',
        'views/arc_eval_launch_wizard_views.xml',
        'views/menu_items.xml',
    ],
    'demo': [],
}
