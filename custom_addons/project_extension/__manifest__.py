{
    'name': 'Project Extension',
    'version': '1.0',
    'depends': ['base', 'project', 'mail', 'hr', 'odoo_google_meet_integration'],
    'data': [
        'security/ir.model.access.csv',
        'data/mail_template.xml',
        'views/project_view.xml',
        'views/designation_views.xml',
        'data/designation_data.xml',
        'data/project_data.xml'
    ],
    'installable': True,
    'application': False,
}
