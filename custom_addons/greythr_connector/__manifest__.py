{
    'name': 'greytHR Connector',
    'version': '19.0.1.0.0',
    'summary': 'Sync employees, leave balances and leave transactions from greytHR into Odoo.',
    'description': """
greytHR Connector
=================
Connects Odoo to the greytHR HRMS platform and pulls:

* Employees (mapped to ``hr.employee`` via ``employee_code``)
* Leave types
* Leave balances (per employee, per leave type, per year)
* Leave transactions (applied, approved, rejected, cancelled)

Provides a full connector experience: connection instance with credentials,
test connection button, manual sync buttons for each data type and a daily
cron for automatic synchronisation.
    """,
    'author': 'Ethara AI',
    'website': 'https://ethara.ai',
    'category': 'Human Resources/HR',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'hr',
        'hr_holidays',
        'mail',
        'ethara_hrms_extension',
    ],
    'external_dependencies': {
        'python': ['requests'],
    },
    'data': [
        'security/greythr_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/greythr_instance_views.xml',
        'views/greythr_employee_views.xml',
        'views/greythr_leave_type_views.xml',
        'views/greythr_leave_balance_views.xml',
        'views/greythr_leave_transaction_views.xml',
        'views/greythr_leave_request_views.xml',
        'views/greythr_payroll_views.xml',
        'views/hr_employee_views.xml',
        'views/greythr_menu.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
