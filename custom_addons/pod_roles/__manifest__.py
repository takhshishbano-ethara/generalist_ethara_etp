{
    'name': 'Pod Roles',
    'version': '19.0.1.0.0',
    'category': 'Tools',
    'summary': 'Defines Admin, PM (Programme Manager), PL (Pod Lead) and Tasker API roles',
    'depends': ['api_auth_gateway'],
    'data': [
        'data/api_role_data.xml',
        'data/api_role_line_data.xml',
        'data/pod_roles_sync.xml',
    ],
    'installable': True,
}
