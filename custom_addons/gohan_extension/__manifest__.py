{
    "name": "Gohan Extension",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "Role-scoped analytics dashboard REST API for Gohan PRD jobs.",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "gohan",
        "api_auth_gateway",
        "task_forge_bridge",
        "project_extension",
        # Defines project.project.project_classification / connected_table /
        # api_map_ids and the etp.external.project.api.map model that
        # data/gohan_project_data.xml writes to.
        "etp_projects",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/gohan_project_data.xml",
        "data/batch_delivery_data.xml",
        "views/batch_delivery_views.xml",
    ],
    "installable": True,
    "application": False,
}
