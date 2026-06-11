{
    "name": "Assessment Extension (PEN Workflow APIs)",
    "summary": "Backend APIs for the PEN-driven assessment authoring workflow.",
    "description": """
Backend APIs that implement the PEN workflow modules:

* MOD-View-System-Prompt  (read-only)
* MOD-Review-Question     (Eval Compare + Prompt Writing variants)
* MOD-Lock-Confirm        (assessment-level question-bank lock)
* MOD-Schedule-Confirm    (send / go-live)

All endpoints are namespaced under ``/api/v1/assessment_extension/`` and
reuse the ``etp_assessment`` groups + ``api_auth_gateway`` decorators.
No mutations are performed on the base module; this addon only
``_inherit``-extends a few fields on ``etp.assessment`` /
``etp.assessment.question`` and adds four new models.
""",
    "version": "19.0.1.0.0",
    "category": "Human Resources/Assessment",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "hr",
        "mail",
        "etp_assessment",
        "api_auth_gateway",
    ],
    "data": [
        "mod_security/ir.model.access.csv",
        "mod_data/eval_compare_dimensions_data.xml",
        "mod_data/system_prompt_data.xml",
    ],
    "application": False,
    "installable": True,
    "auto_install": False,
}
