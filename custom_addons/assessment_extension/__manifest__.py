# -*- coding: utf-8 -*-
{
    "name": "Assessment Extension",
    "version": "19.0.1.0.0",
    "category": "Human Resources",
    "summary": (
        "REST API for the assessment monitoring screens: per-candidate result "
        "(SCR-096), item analysis + distribution drawer (SCR-097), CTO override "
        "approvals (SCR-098), candidate history (SCR-099), score override "
        "(MOD-Score-Override)."
    ),
    "description": """
Backend (models + JSON endpoints) for five surfaces of the Assessment design
package (/Users/alok/Egon/assessment):

  * SCR-096 — Candidate Result drill-in (per-day index + per-question REVIEW)
  * SCR-097 — Item Analysis (per-question cohort performance)
  * SCR-097 — Distribution Drawer (per-question histogram + flag-for-regen)
  * SCR-098 — CTO Override Approvals (self-case escalation inbox)
  * SCR-099 — Candidate History (one candidate's results across assessments)
  * MOD-Score-Override — Override commit (direct OR self-case → CTO request)

Adds the fields ASSESSMENT-WORKFLOW.md §12 requires on etp.assessment /
etp.assessment.question / etp.assessment.evaluator and introduces three
new models — etp.assessment.day.session, etp.assessment.submission,
etp.assessment.override.request — that back the §6 scoring, §6.4 override,
§6.5 self-case guard, and §6.6 aggregation flows.

All HTTP endpoints follow the api_auth_gateway return_Response envelope and
are guarded by @validate_token, consistent with etp_assessment_extension /
aurora_extension / crowley_extension.

UI for these surfaces is OUT OF SCOPE in this addon — the SCR-096..099 / MOD
screens are built by the frontend team against this API.
    """,
    "author": "Ethara",
    "website": "https://www.ethara.com",
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
        "security/assessment_extension_security.xml",
        "security/ir.model.access.csv",
        "data/sequences.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
