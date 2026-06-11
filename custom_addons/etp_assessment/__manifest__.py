# -*- coding: utf-8 -*-
{
    "name": "ETP Assessment",
    "summary": "Question Bank & Assessment module for ETP evaluations",
    "description": """
Manages question bank master with configurable dimensions and options.
Supports creating assessments, assigning candidates, and collecting responses.
Includes a public portal for candidates to take assessments via email link.
    """,
    "author": "Ethara",
    "website": "https://www.ethara.com",
    "category": "Human Resources",
    "version": "19.0.0.9",
    "application": True,
    "depends": ["base", "web", "hr", "mail", "website"],
    "data": [
        "security/etp_assessment_security.xml",
        "security/ir.model.access.csv",
        "data/default_data.xml",
        "data/llm_config_parameters.xml",
        "data/mail_template.xml",
        "views/category_views.xml",
        "views/dimension_views.xml",
        "views/question_views.xml",
        "views/assessment_views.xml",
        "views/question_import_views.xml",
        "views/portal_templates.xml",
        "views/menu_items.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js",
            "etp_assessment/static/src/dashboard/dashboard.js",
            "etp_assessment/static/src/dashboard/dashboard.xml",
            "etp_assessment/static/src/dashboard/dashboard.scss",
            "etp_assessment/static/src/prompt/prompt.js",
            "etp_assessment/static/src/prompt/prompt.xml",
        ],
    },
    "demo": [],
}
