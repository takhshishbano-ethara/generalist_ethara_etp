{
    "name": "ETP Assessment Pro",
    "version": "19.0.1.123.0",
    "summary": "SOP-direct Question Bank + Assessment lifecycle (LLM-driven, Vertex AI)",
    "author": "Ethara",
    "website": "https://www.ethara.com",
    "category": "Human Resources",
    "license": "LGPL-3",
    "depends": ["base", "mail", "hr", "hr_recruitment", "employee_extension",
                "website", "auth_signup"],
    "data": [
        "security/etp_assessment_security.xml",
        "security/ir.model.access.csv",
        "security/etp_assessment_record_rules.xml",
        "views/question_views.xml",
        "views/bank_import_views.xml",
        "views/prompt_views.xml",
        "views/res_config_settings_views.xml",
        "views/assessment_views.xml",
        "views/hr_applicant_views.xml",
        "views/analytics_views.xml",
        "views/dashboard_views.xml",
        "views/sop_ranking_views.xml",
        "views/llm_dashboard_views.xml",
        "views/tag_views.xml",
        "views/portal_templates.xml",
        "data/cron.xml",
        "data/mail_template.xml",
        "views/menus.xml",
        "views/llm_usage_views.xml",
    ],
    "external_dependencies": {
        # M-3: pin Pillow to the libwebp-CVE-patched line (CVE-2023-4863 class).
        # The runtime decompression-bomb cap lives in services/imaging.py
        # (_harden_pillow sets MAX_IMAGE_PIXELS); this floors the library version
        # so a stale base image cannot ship the vulnerable libwebp build.
        "python": ["PyJWT", "httpx", "boto3", "cryptography", "defusedxml",
                   "Pillow>=10.3.0"],
    },
    "assets": {
        "web.assets_frontend": [
            "etp_assessment_pro/static/src/scss/portal.scss",
        ],
        "web.assets_backend": [
            "etp_assessment_pro/static/src/scss/backend.scss",
            "etp_assessment_pro/static/src/list/prompt_import_list.js",
            "etp_assessment_pro/static/src/list/prompt_import_list.xml",
            "etp_assessment_pro/static/src/fields/upload_button/upload_button.js",
            "etp_assessment_pro/static/src/fields/upload_button/upload_button.xml",
            "etp_assessment_pro/static/src/fields/file_chips/file_chips.js",
            "etp_assessment_pro/static/src/fields/file_chips/file_chips.xml",
        ],
    },
    "installable": True,
    "application": True,
}
