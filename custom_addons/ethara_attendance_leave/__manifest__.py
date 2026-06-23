{
    'name': 'Ethara AI Attendance & Leave Management',
    'version': '19.0.1.0.0',
    'category': 'Human Resources',
    'summary': 'Custom attendance rules, leave types (SL/CL/EL/LOP), accruals, sandwich rule',
    'description': """
        Ethara AI Attendance & Leave Management
        ========================================
        - Flexi-time check-in (9:30-10:00 AM), grace period (10:30 AM max)
        - Late arrival penalty: 4th late in a month = 0.5 day deduction
        - Minimum 8 productive hours/day with weekly deficit tracking
        - Four leave types: Sick Leave, Casual Leave, Earned Leave, Loss of Pay (LOP)
        - Monthly accrual (1 day/month each), year-end lapse (SL/CL), EL carry-forward (max 7 days)
        - Sandwich rule for CL (Fri+Mon = Sat+Sun counted)
        - Medical certificate requirement for SL > 2 consecutive days
        - LOP recommendation when SL/CL/EL balances are exhausted
        - EL encashment on separation
        - Absence alert: 5+ consecutive unauthorized days → HR notification
        - WFH restriction
    """,
    'author': 'Ethara',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'hr',
        'hr_attendance',
        'hr_holidays',
        'mail',
        'api_auth_gateway',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/leave_type_data.xml',
        'data/ir_cron_data.xml',
        'views/hr_employee_views.xml',
        'views/hr_leave_views.xml',
        'views/hr_attendance_views.xml',
        'views/leave_management_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
