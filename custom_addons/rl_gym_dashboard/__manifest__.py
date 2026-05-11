{
    'name': 'RL Training Dashboard',
    'version': '19.0.2.0.0',
    'category': 'Productivity',
    'summary': 'RL Model Training Platform with 6-Step Wizard',
    'description': """
RL Training Platform
====================

A comprehensive ML model training platform with:
- 6-step training wizard (Model Selection → Configuration → Training → Metrics → Weights → Inference)
- Real-time training monitoring dashboard
- HuggingFace dataset integration
- S3 weight management
- Simulated training with realistic metrics
- Chart.js visualizations

Features:
- Multi-step wizard with stepper UI
- Live updating Chart.js graphs
- KPI cards for key training metrics
- HuggingFace dataset search and preview
- LoRA/GTPO/Curriculum configuration
- S3 upload for trained weights
- Inference playground
    """,
    'author': 'Ethara',
    'website': 'https://ethara.ai',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'web',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/seed_models.xml',
        'views/rl_training_job_views.xml',
        'views/rl_gym_dashboard_menus.xml',
    ],
    'post_init_hook': 'seed_demo_data',
    'assets': {
        'web.assets_backend': [
            'rl_gym_dashboard/static/src/scss/rl_dashboard.scss',
            'rl_gym_dashboard/static/src/components/chart_utils.js',
            'rl_gym_dashboard/static/src/components/runs_dashboard/runs_dashboard.scss',
            'rl_gym_dashboard/static/src/components/runs_dashboard/runs_dashboard.js',
            'rl_gym_dashboard/static/src/components/runs_dashboard/runs_dashboard.xml',
            'rl_gym_dashboard/static/src/components/dashboard/dashboard.js',
            'rl_gym_dashboard/static/src/components/dashboard/dashboard.xml',
            'rl_gym_dashboard/static/src/components/training_wizard/training_wizard.scss',
            'rl_gym_dashboard/static/src/components/training_wizard/training_wizard.js',
            'rl_gym_dashboard/static/src/components/training_wizard/training_wizard.xml',
            'rl_gym_dashboard/static/src/components/training_wizard/step_model_selection.js',
            'rl_gym_dashboard/static/src/components/training_wizard/step_configuration.js',
            'rl_gym_dashboard/static/src/components/training_wizard/step_training.js',
            'rl_gym_dashboard/static/src/components/training_wizard/step_metrics.js',
            'rl_gym_dashboard/static/src/components/training_wizard/step_weights.js',
            'rl_gym_dashboard/static/src/components/training_wizard/step_inference.js',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
