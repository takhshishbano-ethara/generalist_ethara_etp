{
    'name': 'Huskarl Dashboard',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Agent Evaluation Framework Dashboard',
    'description': """
        Huskarl Dashboard — Harbor Framework
        =====================================
        Interactive dashboard showcasing the Harbor framework for evaluating
        and optimizing AI agents and language models in sandboxed container
        environments. Agents must navigate codebases, write code, and pass
        test suites — all within Docker or cloud-based containers.
    """,
    'author': 'Ethara',
    'website': 'https://github.com/harbor-framework/harbor',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'web',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/huskarl_dashboard_menus.xml',
        'views/portal_templates.xml',
    ],
    'assets': {
        # Backend bundle (OWL component + SCSS) — loaded when an
        # authenticated user opens the "Huskarl" app/menu.
        'web.assets_backend': [
            'huskarl_dashboard/static/src/scss/huskarl_showcase.scss',
            'huskarl_dashboard/static/src/components/showcase/showcase.js',
            'huskarl_dashboard/static/src/components/showcase/showcase.xml',
        ],
        # Public /huskarl page assets are NOT bundled into
        # web.assets_frontend on purpose — the portal template serves
        # them as bare <link>/<script> tags so Bootstrap and other
        # portal chrome do not override the editorial design.
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
