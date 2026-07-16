{
    'name': 'Ethara Project',
    'version': '19.0.1.0.0',
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
        'views/ethara_project_views.xml',
        'views/ethara_project_menu.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
