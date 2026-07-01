{
    'name': 'Wiki Core',
    'version': '19.0.1.2.0',
    'category': 'Human Resources',
    'summary': 'Employee wiki: FAQs, holidays, grievances, training, '
               'leave summary and org chart APIs for the employee portal.',
    'author': 'Ethara',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'hr',
        'hr_holidays',
        'mail',
        'notification',
        'api_auth_gateway',
        'ethara_attendance_leave',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/wiki_data.xml',
        'views/wiki_views.xml',
        'views/wiki_feedback_views.xml',
        'data/wiki_demo_data.xml',
    ],
    'installable': True,
    'application': True,
}
