{
    'name': 'Kraken Dashboard',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Repository-level Performance Optimization Benchmark Dashboard',
    'description': """
        Kraken Dashboard - SWE-fficiency Methodology
        =============================================
        Interactive dashboard showcasing the Kraken benchmark for evaluating
        LM agents on runtime performance optimization of real-world Python
        repositories. Agents must localize bottlenecks and produce patches
        that exceed expert-level speedup while passing all unit tests.
    """,
    'author': 'Ethara',
    'website': 'https://github.com/Ethara-Ai/Kraken-Dataset',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'portal',
        'website',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/kraken_dashboard_menus.xml',
        'views/portal_templates.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'kraken_dashboard/static/src/scss/kraken_showcase.scss',
            'kraken_dashboard/static/src/components/showcase/showcase.js',
            'kraken_dashboard/static/src/components/showcase/showcase.xml',
        ],
        'web.assets_frontend': [
            'kraken_dashboard/static/src/portal/css/kraken_portal.css',
            'kraken_dashboard/static/src/portal/js/kraken_portal.js',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
