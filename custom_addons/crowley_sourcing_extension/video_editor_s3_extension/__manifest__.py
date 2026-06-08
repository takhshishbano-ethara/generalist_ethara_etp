{
    "name": "Crowley Sourcing Extension",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "Task Forge REST API + state-change notifications for Crowley Sourcing (video_editor_s3).",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "video_editor_s3",
        "notification",
        "api_auth_gateway",
        "project_extension",
        "etp_projects",
    ],
    "data": [
        "data/video_editor_project_views.xml",
    ],
    "installable": True,
    "application": False,
}
