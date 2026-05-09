{
    'name': 'RL Training Dashboard',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Real-time Reinforcement Learning Training Monitoring Dashboard',
    'description': """
RL Training Environment Monitoring Dashboard
=============================================

A comprehensive real-time monitoring dashboard for RL training experiments.
Displays live metrics including rewards, losses, episode statistics,
gradient norms, and training progress with simulated demo data.

Features:
- Live updating Chart.js graphs tied to universal clock
- KPI cards for key training metrics
- Episode history table
- Training loss curves (policy, value, entropy)
- Reward progression and moving averages
- Hyperparameter configuration panel
- Alert/anomaly detection indicators
    """,
    'author': 'Ethara',
    'website': 'https://ethara.ai',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'web',
    ],
    'data': [
        'views/rl_gym_dashboard_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'rl_gym_dashboard/static/src/scss/rl_dashboard.scss',
            'rl_gym_dashboard/static/src/components/dashboard/dashboard.js',
            'rl_gym_dashboard/static/src/components/dashboard/dashboard.xml',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
