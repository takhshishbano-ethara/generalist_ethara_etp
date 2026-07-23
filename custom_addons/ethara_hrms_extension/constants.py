"""Single source of truth for pipeline-status keys, labels, and resume
recommendation mappings shared across ``ethara_hrms_extension``.

Both ``controllers/candidates.py`` and ``controllers/screening.py`` must
import from here — never redefine locally, and never introduce a
parallel "fine-grained stage enum" alongside these keys. The 9 values
below mirror ``hr.applicant.pipeline_status`` verbatim: 8 progressive
buckets rendered as steps in the frontend tracker, plus ``rejected`` as
a terminal state that replaces the current step.
"""

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

PIPELINE_STATUS_KEYS = tuple(k for k in PIPELINE_STATUS_LABELS if k != "rejected")

STATUS_TO_PIPELINE_BUCKET = {s: bucket for bucket, keys in {
    "applied":     ("pending",),
    "shortlisted": ("resume_screening_in_progress", "resume_screening_rejected"),
    "assessment":  ("resume_screening_passed", "pending_assessment", "assessment_in_progress", "assessment_pending_review", "assessment_rejected"),
    "evaluation":  ("assessment_passed", "pi_scheduled", "pi_completed", "pi_hold", "pi_rejected"),
    "submission":  ("pi_selected", "documents_requested", "documents_submitted", "documents_rejected"),
    "contract":    ("document_verification_done", "contract_sent", "contract_declined"),
    "compliance":  ("contract_signed",),
    "onboarded":   ("onboard",),
}.items() for s in keys}

REJECTED_HIRING_STATUSES = frozenset({
    "resume_screening_rejected",
    "assessment_rejected",
    "pi_rejected",
    "documents_rejected",
    "contract_declined",
})

_IN_ASSESSMENT_STATUSES = frozenset(
    s for s, bucket in STATUS_TO_PIPELINE_BUCKET.items()
    if bucket == "assessment" and s != "resume_screening_passed"
)

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
