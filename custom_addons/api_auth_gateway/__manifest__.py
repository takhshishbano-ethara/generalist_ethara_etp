{
    'name': 'API Auth Gateway',
    'version': '19.0.1.0.0',
    'category': 'Tools',
    'depends': ['base', 'auth_signup', 'web', 'etp_user_roles'],
    'data': [
        'security/ir.model.access.csv',
        'views/api_role_views.xml',
        'views/res_users_views.xml',
        'data/api_role_data.xml',
    ],
    'installable': True,
}
