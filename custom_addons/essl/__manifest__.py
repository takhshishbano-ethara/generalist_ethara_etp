# -*- coding: utf-8 -*-
{
    "name": "ESSL Biometric Attendance",
    "version": "19.0.3.2.0",
    "category": "Human Resources",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["hr_attendance", "hr", "mail"],
    "external_dependencies": {"python": ["pyodbc"]},
    "data": [
        "security/essl_security.xml",
        "security/ir.model.access.csv",
        "data/api_config.xml",
        "views/essl_attendance_log_views.xml",
        "views/essl_server_views.xml",
        "views/essl_device_views.xml",
        "views/essl_daily_summary_views.xml",
        "views/essl_menu.xml",
    ],
    "application": True,
    "installable": True,
}
