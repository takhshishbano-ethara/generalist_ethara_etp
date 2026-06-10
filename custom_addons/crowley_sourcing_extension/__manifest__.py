{
    "name": "Crowley Sourcing Extension",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "Role-scoped analytics dashboard REST API for Crowley Sourcing video projects.",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "video_editor_s3",
        "api_auth_gateway",
        "task_forge_bridge",
        "project_extension",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/batch_delivery_data.xml",
        "views/batch_delivery_views.xml",
    ],
    "installable": True,
    "application": False,
}
