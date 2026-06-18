{
    'name': 'ETP Projects',
    'version': '19.0.1.0.13',
    'category': 'Project',
    'summary': 'Classify projects as internal or external',
    'description': """
ETP Projects
============
Adds an Internal/External classification to projects.

* Internal projects appear in the base Project app and its Task menus.
* External projects are managed from a dedicated External Projects app.
""",
    'depends': ['project', 'project_extension', 'mail', 'api_auth_gateway', 'etp_user_roles', 'task_forge_bridge'],
    'data': [
        'security/ir.model.access.csv',
        'data/sequence_data.xml',
        'data/etp_ai_model_data.xml',
        'data/etp_infra_type_data.xml',
        'data/mail_template_data.xml',
        'views/project_team_csv_import_views.xml',
        'views/project_views.xml',
        'views/external_project_views.xml',
        'views/api_mapping_views.xml',
        'views/etp_ai_model_views.xml',
        'wizards/batch_budget_daily_task_wizard_views.xml',
        'wizards/batch_budget_request_wizard_views.xml',
        'views/batch_budget_views.xml',
        'views/aws_budget_views.xml',
        'views/batch_budget_request_views.xml',
        'views/project_budget_topup_views.xml',
    ],
    'external_dependencies': {'python': ['boto3', 'google-cloud-bigquery', 'openpyxl']},
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
