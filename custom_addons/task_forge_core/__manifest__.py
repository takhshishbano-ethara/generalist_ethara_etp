{
    'name': 'Task Forge Core',
    'version': '19.0.1.0.1',
    'category': 'Project',
    'summary': 'Task logging, blocker escalation, bug tracking, analytics for Task Forge',
    'description': """
        Core Task Forge module providing:
        - Task logging with start/end lifecycle and screenshots
        - Multi-step blocker escalation (Tasker -> QR -> PL -> Admin)
        - Validated bug tracking
        - Bug reports with video
        - Flags with severity levels
        - File management via S3
        - Analytics dashboards and KPIs
        - CSV/XLSX export
        - Inactivity cron job
    """,
    'author': 'Ethara',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'hr',
        'project',
        'mail',
        'etp_user_roles',
        'api_auth_gateway',
        'notification',
        's3_connector',
        'project_extension',
        'task_forge_bridge',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'data/ir_sequence_data.xml',
        'data/ir_cron_data.xml',
        'views/task_log_views.xml',
        'views/blocker_views.xml',
        'views/validated_bug_views.xml',
        'views/bug_report_views.xml',
        'views/flag_views.xml',
        'views/res_config_settings_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
