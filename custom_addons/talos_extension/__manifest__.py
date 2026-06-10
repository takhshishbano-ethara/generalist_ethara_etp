{
    "name": "Talos Extension",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "Role-scoped analytics dashboard REST API for Talos tasks.",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "talos", "api_auth_gateway", "etp_user_roles"],
    "data": [
        "security/ir.model.access.csv",
        "data/batch_delivery_data.xml",
        "views/batch_delivery_views.xml",
    ],
    "installable": True,
    "application": False,
}
