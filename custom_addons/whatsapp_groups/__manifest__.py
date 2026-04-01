# -*- coding: utf-8 -*-
{
    'name': "WhatsApp Groups",

    'summary': "Create and manage WhatsApp groups via Meta Cloud API",

    'description': """
        Create, manage and delete WhatsApp groups using the official
        Meta WhatsApp Business Cloud API (v23.0).

        Features:
        - Create groups with subject and description
        - Generate and reset invite links
        - Track group participants
        - Webhook support for group lifecycle events
        - Join approval mode (auto/admin)
    """,

    'author': "Custom",
    'website': "",

    'category': 'Discuss',
    'version': '19.0.1.0.0',
    'application': True,
    'license': 'LGPL-3',

    'depends': ['base', 'mail'],

    'data': [
        'security/ir.model.access.csv',
        'views/whatsapp_group_account_views.xml',
        'views/whatsapp_group_views.xml',
        'views/whatsapp_group_menus.xml',
    ],

    'demo': [],
}
