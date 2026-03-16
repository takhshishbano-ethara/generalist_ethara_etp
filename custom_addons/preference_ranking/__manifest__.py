# -*- coding: utf-8 -*-
{
    "name": "preference_ranking",
    "summary": "Short (1 phrase/line) summary of the module's purpose",
    "description": """
Long description of module's purpose
    """,
    "author": "My Company",
    "website": "https://www.yourcompany.com",
    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/15.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    "category": "Uncategorized",
    "version": "19.0.0.1",
    "application": True,
    # any module necessary for this one to work correctly
    "depends": ["base", "web", "hr", "base_user_role", "portal"],
    # always loaded
    "data": [
        "security/vindex_security.xml",
        "data/vindex_user_roles.xml",
        "security/ir.model.access.csv",
        "data/data.xml",
        "views/views.xml",
        "views/token_views.xml",
        "views/templates.xml",
        "views/menu_item.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "preference_ranking/static/src/views/fields/markdown_latex_field/markdown_latex_field.js",
            "preference_ranking/static/src/views/fields/markdown_latex_field/markdown_latex_field.xml",
            "preference_ranking/static/src/views/fields/markdown_latex_field/markdown_latex_field.scss",
            "preference_ranking/static/src/views/widgets/evaluate_button/evaluate_button.js",
            "preference_ranking/static/src/views/widgets/evaluate_button/evaluate_button.xml",
            "preference_ranking/static/src/views/widgets/evaluate_button/evaluate_button.scss",
        ],
        "web.assets_frontend": [
            "preference_ranking/static/src/portal/css/portal.css",
            "preference_ranking/static/src/portal/js/portal.js",
        ],
    },
    # only loaded in demonstration mode
    "demo": [
        "demo/demo.xml",
    ],
}
