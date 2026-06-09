{
    "name": "Talos Extension",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "Role-scoped overview/task/team REST API for Talos LLM tasks.",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "talos",
        "api_auth_gateway",
        "etp_user_roles",
    ],
    "data": [
        "security/ir.model.access.csv",
    ],
    "installable": True,
    "application": False,
}
