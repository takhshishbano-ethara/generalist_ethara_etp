{
    "name": "Workflow Management",
    "version": "19.0.1.0.0",
    "category": "Human Resources/Seating",
    "summary": "Complete HRMS with seat management, attendance tracking, room booking, and transfer approvals",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "hr",
        "web",
        "mail",
    ],
    "data": [
        # Security (must load first)
        "security/workflow_groups.xml",
        "security/ir.model.access.csv",
        # Data
        "data/ir_cron.xml",
        # Views
        "views/floor_views.xml",
        "views/seat_views.xml",
        "views/employee_views.xml",
        "views/room_booking_views.xml",
        "views/seat_transfer_views.xml",
        "views/audit_log_views.xml",
        "views/attendance_sync_views.xml",
        "views/menu_items.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
