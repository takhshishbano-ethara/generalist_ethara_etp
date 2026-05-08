{
    'name': 'Pax Dashboard',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Security Vulnerability Assessment Dashboard',
    'description': """
        Pax Dashboard - Security Analysis Methodology
        =============================================
        Interactive dashboard showcasing the Pax benchmark for evaluating
        AI agents on security vulnerability detection and patching in Python
        repositories. Agents must identify vulnerabilities and produce patches
        that exceed expert-level security fixes while passing all tests.
    """,
    'author': 'Ethara',
    'website': 'https://github.com/Ethara-Ai/Pax-Dataset',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'web',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/pax_dashboard_menus.xml',
        'views/portal_templates.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'pax_dashboard/static/src/scss/pax_showcase.scss',
            'pax_dashboard/static/src/components/showcase/showcase.js',
            'pax_dashboard/static/src/components/showcase/showcase.xml',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
