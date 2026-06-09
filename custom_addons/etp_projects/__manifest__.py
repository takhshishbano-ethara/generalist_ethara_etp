{
    'name': 'ETP Projects',
    'version': '1.0',
    'category': 'Project',
    'summary': 'Classify projects as internal or external',
    'description': """
ETP Projects
============
Adds an Internal/External classification to projects.

* Internal projects appear in the base Project app and its Task menus.
* External projects are managed from a dedicated External Projects app.
""",
    'depends': ['project', 'project_extension', 'mail', 'api_auth_gateway', 'etp_user_roles'],
    'data': [
        'security/ir.model.access.csv',
        'data/sequence_data.xml',
        'data/mail_template_data.xml',
        'views/project_views.xml',
        'views/external_project_views.xml',
        'views/api_mapping_views.xml',
        'views/aws_budget_views.xml',
        'views/token_purchase_request_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'external_dependencies': {'python': ['boto3']},
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
