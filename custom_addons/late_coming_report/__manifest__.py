{
    'name': 'Late Coming Report & HR Alerts',
    'version': '19.0.1.0.0',
    'category': 'Human Resources',
    'summary': 'Daily late-comer report and monthly late-limit alerts emailed to HR',
    'description': """
        Late Coming Report & HR Alerts
        ==============================
        Office start 10:00 AM IST, grace until 10:30 AM IST. An employee is a
        LATE COMER if their FIRST check-in of the day (attendance check_in is
        stored in UTC and converted to IST = UTC + 5:30) is after 10:30 AM IST.

        - Daily cron at 11:00 AM IST emails a late-comers report to all users
          whose role type (api.role.user_type) is 'hr'.
        - Each employee may be late at most 3 times per calendar month. When an
          employee exceeds that limit, an additional alert email is sent to HR.

        Self-contained: does NOT modify any existing module. Computes lateness
        directly from hr.attendance.check_in (no dependency on other late fields).
    """,
    'author': 'Ethara',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'hr',
        'hr_attendance',
        'mail',
        'api_auth_gateway',
        's3_connector',
        'employee_role_import',
    ],
    'data': [
        'data/ir_cron_data.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
