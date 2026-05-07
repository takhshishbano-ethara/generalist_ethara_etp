{
    'name': 'Surtor Dashboard',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Security Vulnerability Assessment Dashboard',
    'description': """
        Surtor Dashboard - Security Analysis Methodology
        =============================================
        Interactive dashboard showcasing the Surtor benchmark for evaluating
        AI agents on security vulnerability detection and patching in Python
        repositories. Agents must identify vulnerabilities and produce patches
        that exceed expert-level security fixes while passing all tests.
    """,
    'author': 'Ethara',
    'website': 'https://github.com/Ethara-Ai/Surtor-Dataset',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'web',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/surtor_dashboard_menus.xml',
        'views/portal_templates.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'surtor_dashboard/static/src/scss/surtor_showcase.scss',
            'surtor_dashboard/static/src/components/showcase/showcase.js',
            'surtor_dashboard/static/src/components/showcase/showcase.xml',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
