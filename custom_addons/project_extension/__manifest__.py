{
    'name': 'Project Extension',
    'version': '1.0',
    'depends': ['base', 'project', 'mail', 'hr', 'odoo_google_meet_integration','employee_extension'],
    'data': [
        'security/ir.model.access.csv',
        'data/mail_template.xml',
        'views/project_view.xml',
        'views/designation_views.xml',
        'views/project_blocker.xml',
        'views/task_template_view.xml',
        'data/ir_sequence_data.xml',
        'data/designation_data.xml',
        'data/project_data.xml',
        'data/ir_cron_data.xml'
    ],
    'installable': True,
    'application': False,
}
