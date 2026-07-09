# -*- coding: utf-8 -*-
{
    'name': "Documenso Connector",
    'version': '19.0.2.0.0',
    'category': 'Human Resources/Document Management',
    'summary': "Documenso e-signature integration: templates, employee contracts, webhook.",
    'description': """
Documenso Connector
===================
Connect Odoo to a Documenso instance (self-hosted or cloud) via API token.

Features
--------
* Configure Documenso API URL, token, signing URL and webhook secret from Settings.
* Cache Documenso templates and their fields locally.
* Send contracts to employees via multi-select templates (Offer + NDA + Employment, etc.).
* Track sent contracts per employee in a dedicated table, mapped to hr.employee.
* Receive completion events via webhook and via scheduled sync.
* Store signed PDFs and every filled field value against the contract.
* Direct View / Download links plus per-envelope-item downloads for bundles.
    """,
    'author': 'Ethara AI',
    'company': 'Ethara AI',
    'maintainer': 'Ethara AI',
    'website': 'https://ethara.ai',
    'depends': ['base', 'mail', 'hr', 'api_auth_gateway'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/documenso_template_views.xml',
        'views/documenso_contract_views.xml',
        'views/documenso_signed_profile_views.xml',
        'views/documenso_document_views.xml',
        'views/documenso_recipient_views.xml',
        'views/documenso_field_views.xml',
        'views/hr_employee_views.xml',
        'wizard/documenso_sync_wizard_views.xml',
        'wizard/documenso_upload_wizard_views.xml',
        'wizard/documenso_send_contract_wizard_views.xml',
        'views/documenso_menu.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
    'auto_install': False,
}
