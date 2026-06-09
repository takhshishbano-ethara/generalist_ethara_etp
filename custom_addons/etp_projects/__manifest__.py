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
    'depends': ['project', 'project_extension', 'mail', 'api_auth_gateway'],
    'data': [
        'security/ir.model.access.csv',
        'views/project_views.xml',
        'views/external_project_views.xml',
        'views/api_mapping_views.xml',
        'views/aws_budget_views.xml',
    ],
    'external_dependencies': {'python': ['boto3']},
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
