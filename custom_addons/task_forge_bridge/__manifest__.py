{
    'name': 'Task Forge Bridge',
    'version': '19.0.1.0.0',
    'category': 'Human Resources',
    'summary': 'Extends HR modules for Task Forge integration',
    'description': """
        Extends hr.employee, hr.attendance, hr.leave, and project.project
        to support Task Forge task management system.
    """,
    'author': 'Ethara',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'hr',
        'hr_attendance',
        'hr_holidays',
        'project',
        'etp_user_roles',
        'api_auth_gateway',
        'notification',
        's3_connector',
        'project_extension',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'views/hr_employee_views.xml',
        'views/hr_attendance_views.xml',
        'views/hr_leave_views.xml',
        'views/project_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
