{
    'name': 'Kubera Notifications',
    'version': '1.0.0',
    'summary': 'Custom notification system for Kubera',
    'description': """
        This module provides a flexible notification system with:
        - Title, message, priority
        - Read/Unread tracking
        - Linked model using res_model + res_id
        - User-specific notifications
        - Notification creation function
    """,
    'author': 'hyper grocers private limited',
    # 'website': 'https://yourwebsite.com',
    'license': 'LGPL-3',
    'category': 'Tools',
    'depends': ['base'],
    'data': [
        'views/kubera_notification_views.xml',
        'security/ir.model.access.csv',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
