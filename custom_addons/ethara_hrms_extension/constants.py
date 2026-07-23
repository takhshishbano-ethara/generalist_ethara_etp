"""Single source of truth for pipeline-status keys, labels, and resume
recommendation mappings shared across ``ethara_hrms_extension``.

Both ``controllers/candidates.py`` and ``controllers/screening.py`` must
import from here — never redefine locally, and never introduce a
parallel "fine-grained stage enum" alongside these keys. The 9 values
below mirror ``hr.applicant.pipeline_status`` verbatim: 8 progressive
buckets rendered as steps in the frontend tracker, plus ``rejected`` as
a terminal state that replaces the current step.
"""

PIPELINE_STATUS_KEYS = (
    "applied",
    "shortlisted",
    "assessment",
    "evaluation",
    "submission",
    "contract",
    "compliance",
    "email_id",
    "onboarded",
)

PIPELINE_STATUS_LABELS = {
    "applied":     "Applied",
    "shortlisted": "Shortlisted",
    "assessment":  "Assessment",
    "evaluation":  "Evaluation",
    "submission":  "Submission",
    "contract":    "Contract",
    "compliance":  "Compliance",
    "email_id":    "Email ID",
    "onboarded":   "Onboarded",
    "rejected":    "Rejected",
}

_POST_ASSESSMENT_STATUSES = frozenset({
    "assessment_passed",
    "pi_scheduled", "pi_completed", "pi_selected", "pi_hold", "pi_rejected",
    "documents_requested", "documents_submitted", "documents_rejected",
    "document_verification_done",
    "contract_sent", "contract_declined", "contract_signed",
    "onboard",
})
_IN_ASSESSMENT_STATUSES = frozenset({
    "pending_assessment", "assessment_in_progress",
    "assessment_pending_review", "assessment_rejected",
})

RESUME_RECOMMENDATION_OUT = {
    "shortlist":    "shortlisted",
    "reject":       "rejected",
    "maybe":        "needs_review",
    "needs_review": "needs_review",
}

RESUME_RECOMMENDATION_IN = {
    "shortlisted":  "shortlist",
    "shortlist":    "shortlist",
    "rejected":     "reject",
    "reject":       "reject",
    "needs_review": "needs_review",
    "maybe":        "maybe",
}
