{
    "name": "MM Performance Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Upload CSVs, run performance pipeline, and view HTML dashboards",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web"],
    "data": [
        "security/ir.model.access.csv",
        "views/non_stem_dashboard_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "non_stem_dashboard/static/src/js/dashboard_action.js",
            "non_stem_dashboard/static/src/xml/dashboard_action.xml",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
