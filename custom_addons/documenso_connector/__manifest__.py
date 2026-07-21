# -*- coding: utf-8 -*-
{
    'name': "Documenso Connector",
    'version': '19.0.4.1.0',
    'category': 'Human Resources/Document Management',
    'summary': "Documenso e-signature: upload template, edit in Documenso, publish, send to applicants, capture signed PDF via webhook.",
    'description': """
Documenso Connector
===================
Minimal end-to-end applicant document flow:

* Upload a PDF template with a job and doc class (contract or compliance).
* Edit the template natively in Documenso via embedded editor.
* Publish the template to make it available in the Send wizard.
* Send wizard: pick a job, its published templates cascade in, pick applicants, one click.
* Documenso emails each applicant a signing link.
* Webhook receives the completed document and stores the signed PDF against the sent record.
    """,
    'author': 'Ethara AI',
    'company': 'Ethara AI',
    'maintainer': 'Ethara AI',
    'website': 'https://ethara.ai',
    'depends': ['base', 'mail', 'hr', 'hr_recruitment', 'ethara_hrms_extension', 'api_auth_gateway'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'wizard/documenso_create_template_wizard_views.xml',
        'views/documenso_template_views.xml',
        'views/documenso_contract_views.xml',
        'views/hr_applicant_views.xml',
        'wizard/documenso_send_applicant_wizard_views.xml',
        'views/documenso_menu.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'documenso_connector/static/src/components/template_designer/template_designer.scss',
            'documenso_connector/static/src/components/template_designer/template_designer.js',
            'documenso_connector/static/src/components/template_designer/template_designer.xml',
        ],
    },
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
    'auto_install': False,
}
