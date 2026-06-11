{
    "name": "Iris - Recruitment Screening",
    "version": "19.0.1.1.0",
    "category": "Human Resources/Recruitment",
    "summary": "LLM-powered resume screening, interview guides and scorecards",
    "description": """
Iris — Recruitment Screening
============================
LLM-assisted hiring pipeline for ML roles:

* Upload candidate resume (PDF) + target role (role profiles, HoE seeded)
* LLM forensic screening -> SHIP / HOLD / BLOCK with full markdown record
* Dual second-screener sign-off on every human-visible BLOCK
* HOLD verification-evidence workflow with re-screen + auto-BLOCK cron
* Batch screening with an LLM cross-candidate consistency pass
* JD critique + rewrite pre-stage, take-home assessment reviews
* Clarifying-questions generation for HOLD outreach
* Interview guide generation (5-question steering ladders + rubrics)
* Interviewer notes -> LLM scorecard (Strong Hire .. Strong No Hire)
* Quarterly calibration sessions, versioned tech-date reference table
* Full-CRUD REST API under /api/v1/iris/ (token auth via api_auth_gateway)
* Private S3 resume storage with presigned GET URLs
""",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "mail",
        "s3_connector",
        "api_auth_gateway",
    ],
    "external_dependencies": {
        "python": ["cryptography", "requests", "fitz", "markdown", "boto3"],
    },
    "data": [
        "security/iris_security.xml",
        "security/ir.model.access.csv",
        "data/iris_sequence.xml",
        "data/iris_sequence_v11.xml",
        "data/iris_role_profile_data.xml",
        "data/iris_activity_data.xml",
        "data/ir_cron.xml",
        "data/ir_cron_v11.xml",
        "data/mail_templates.xml",
        "wizards/iris_evidence_wizard_views.xml",
        "wizards/iris_block_signoff_wizard_views.xml",
        "views/iris_screening_views.xml",
        "views/iris_interview_views.xml",
        "views/iris_scorecard_views.xml",
        "views/iris_tech_date_reference_views.xml",
        "views/iris_calibration_views.xml",
        "views/iris_role_profile_views.xml",
        "views/iris_batch_views.xml",
        "views/iris_jd_views.xml",
        "views/iris_assessment_views.xml",
        "views/iris_candidate_views.xml",
        "views/res_config_settings_views.xml",
        "views/iris_menus.xml",
    ],
    "application": True,
    "installable": True,
    "auto_install": False,
}
