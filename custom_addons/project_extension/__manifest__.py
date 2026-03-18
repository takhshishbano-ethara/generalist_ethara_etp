{
    'name': 'Project Extension',
    'version': '1.0',
    'depends': ['base', 'project', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'data/mail_template.xml',
        'views/project_view.xml'
    ],
    'installable': True,
    'application': False,
}