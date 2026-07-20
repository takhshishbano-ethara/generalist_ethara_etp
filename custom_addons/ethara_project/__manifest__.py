{
    'name': 'Ethara Project',
    'version': '19.0.1.2.0',
    'category': 'Project',
    'summary': 'Ethara project registry with role-scoped team assignment and S3-backed attachments',
    'description': """
Ethara Project
==============
Standalone project registry (model: ethara.project) with:

* Client-facing and internal project names, goal, start/end dates
* Team assignment fields filtered by api.role:
    - Assigned TPM   -> role.name = 'TPM'
    - Assigned PL/QL -> role.name in ('PL', 'QC', 'QR')
    - Assigned R&D   -> role.name = 'R&D'
* Multiple attachments per project; each attachment is either a pasted
  URL or an uploaded file that is pushed to S3, with only the resulting
  URL persisted.
* REST endpoints (create / update / list / detail) gated by the shared
  api_auth_gateway access-token flow.
""",
    'author': 'Ethara',
    'depends': [
        'base',
        'mail',
        'hr',
        's3_connector',
        'api_auth_gateway',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/api_endpoint_data.xml',
        'data/mail_template_data.xml',
        'data/hr_department_master.xml',
        'data/sequence_data.xml',
        'data/aws_pricing_config_data.xml',
        'data/infra_provider_data.xml',
        'data/ir_cron_data.xml',
        'views/ethara_project_views.xml',
        'views/ethara_project_catalog_views.xml',
        'views/ethara_project_budget_views.xml',
        'views/ethara_project_phase_views.xml',
        'views/ethara_project_cost_views.xml',
        'views/ethara_project_aws_pricing_views.xml',
        'views/ethara_project_budget_monthly_cost_views.xml',
        'wizards/ethara_project_phase_request_review_wizard_views.xml',
        'wizards/ethara_project_budget_topup_reject_wizard_views.xml',
        'wizards/ethara_project_phase_daily_task_wizard_views.xml',
        'wizards/ethara_project_phase_request_wizard_views.xml',
        'views/ethara_project_topup_views.xml',
        'views/res_config_settings_views.xml',
        'views/ethara_project_dashboard_action.xml',
        'views/ethara_project_menu.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ethara_project/static/src/components/budget_health_dashboard/budget_health_dashboard.js',
            'ethara_project/static/src/components/budget_health_dashboard/budget_health_dashboard.xml',
            'ethara_project/static/src/components/budget_health_dashboard/budget_health_dashboard.scss',
        ],
    },
    'external_dependencies': {'python': ['boto3']},
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
