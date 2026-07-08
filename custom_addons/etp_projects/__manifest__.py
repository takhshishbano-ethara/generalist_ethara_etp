{
    'name': 'ETP Projects',
    'version': '19.0.1.0.14',
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
        'data/ir_config_parameter_aws_pricing_data.xml',
        'data/ir_cron_aws_pricing_data.xml',
        'data/mail_template_data.xml',
        'views/project_team_csv_import_views.xml',
        'views/project_views.xml',
        'views/external_project_views.xml',
        'views/api_mapping_views.xml',
        'views/etp_ai_model_views.xml',
        'views/etp_infra_type_views.xml',
        'views/etp_subscription_views.xml',
        'wizards/batch_budget_daily_task_wizard_views.xml',
        'wizards/batch_budget_request_wizard_views.xml',
        'views/batch_budget_views.xml',
        'views/aws_budget_views.xml',
        'views/aws_pricing_views.xml',
        'views/batch_budget_topup_reason_views.xml',
        'views/batch_budget_request_views.xml',
        'views/project_budget_topup_views.xml',
        'views/res_config_settings_views.xml',
        'views/project_budget_dashboard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'etp_projects/static/src/components/budget_health_dashboard/budget_health_dashboard.scss',
            'etp_projects/static/src/components/budget_health_dashboard/budget_health_dashboard.js',
            'etp_projects/static/src/components/budget_health_dashboard/budget_health_dashboard.xml',
        ],
    },
    'external_dependencies': {'python': ['boto3', 'cryptography', 'google-cloud-bigquery', 'openpyxl']},
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
