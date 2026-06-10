{
    "name": "Vegeta Extension",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "Role-scoped analytics dashboard REST API for Vegeta PRD jobs.",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "vegeta",
        "api_auth_gateway",
        "task_forge_bridge",
        "project_extension",
        # Provides project.project's project_classification / connected_table /
        # api_map_ids / category_url / tasker_url fields used by the
        # auto-created internal Vegeta project (data/vegeta_project_data.xml).
        "etp_projects",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/vegeta_project_data.xml",
    ],
    "installable": True,
    "application": False,
}
