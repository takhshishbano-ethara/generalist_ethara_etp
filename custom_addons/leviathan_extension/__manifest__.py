{
    "name": "Leviathan Extension",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "REST APIs for Leviathan jobs: creation, bulk upload and dashboard analytics.",
    "description": (
        "Adds authenticated REST endpoints on top of the Leviathan module:\n"
        " - POST /api/v1/leviathan_ext/jobs/create               single job from JSON\n"
        " - POST /api/v1/leviathan_ext/jobs/bulk_create          bulk jobs from CSV/XLSX\n"
        " - GET  /api/v1/leviathan_ext/analytics/kpi             consolidated KPI dashboard\n"
        " - GET  /api/v1/leviathan_ext/analytics/status_chart    task-count-by-status chart\n"
        " - GET  /api/v1/leviathan_ext/analytics/submission_trend weekly/monthly/yearly trend\n"
        " - GET  /api/v1/leviathan_ext/analytics/dashboard       consolidated dashboard analytics\n"
        " - GET  /api/v1/leviathan_ext/budget/info               AWS budget KPIs, service breakdown, AHT overview and daily burn graph\n\n"
        "All endpoints reuse the api_auth_gateway access_token mechanism and "
        "require membership of the Leviathan Administrator group "
        "(leviathan.group_leviathan_admin), which is the only group permitted "
        "to create leviathan.job records."
    ),
    "author": "Ethara",
    "depends": [
        "base",
        "web",
        "leviathan",
        "api_auth_gateway",
        "project_extension",
        "task_forge_bridge",
        "etp_projects",
    ],
    "data": [],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
