{
    "name": "Client Deliverables",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "Per-project client deliverable hub: git/excel/drive links, client feedback log, and a token-authenticated REST API to read & update them from the frontend.",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "mail",
        "project",
        "api_auth_gateway",
    ],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/client_deliverable_data.xml",
        "views/client_deliverable_views.xml",
    ],
    "installable": True,
    "application": True,
}
