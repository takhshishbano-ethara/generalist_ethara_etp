{
    "name": "ETP Applicant Assessment",
    "summary": (
        "Job-scoped candidate assessment with client-side AI proctoring "
        "(MediaPipe FaceLandmarker + ObjectDetector), presigned S3 evidence "
        "clips, and answer + warning scoring."
    ),
    "description": """
End-to-end assessment flow for the recruitment pipeline:

  * Each hr.job carries an assessment template (questions, duration,
    pass mark, proctoring rules and per-signal penalties).
  * When a candidate reaches the assessment stage on hr.applicant,
    HR clicks "Send Assessment" -> a per-applicant assessment record
    is created and the candidate is emailed a tokenised portal link.
  * Candidate opens the portal (no login), passes a DPDP/GDPR consent
    dialog, and starts. The browser runs MediaPipe Tasks Vision (WASM)
    locally to flag five integrity signals from the webcam feed:
        - no_face (candidate left frame)
        - other_person (multiple faces visible)
        - look_away (head yaw / pitch beyond limits)
        - mobile_phone (COCO object-detector match)
        - lip_movement (jawOpen blendshape oscillation)
  * On each debounced + cooled-down signal the browser uploads a
    320px JPEG snapshot (multipart to Odoo) and a ~7 s webm clip
    DIRECT to S3 via a presigned POST; a warning row is created for
    scoring.
  * Browser also reports tab_switch / window_change / fullscreen_exit
    events and every media-upload failure to a diagnostics beacon.
  * On submit (or auto-submit on time / violation cap) the record moves
    to scored: objective_score is answer-based, warning_penalty deducts
    from the weighted warning kinds, final_score decides pass/fail.

All AI inference runs in the candidate's browser (self-hosted WASM +
models under static/lib/mediapipe/), so the server sees only small
JSON events and thumbnails and scales linearly with S3 throughput.
    """,
    "author": "Ethara",
    "website": "https://www.ethara.com",
    "license": "LGPL-3",
    "category": "Human Resources/Recruitment",
    "version": "19.0.4.0.0",
    "application": True,
    "installable": True,
    "depends": [
        "base",
        "web",
        "mail",
        "hr",
        "hr_recruitment",
        "website",
        "api_auth_gateway",
    ],
    "external_dependencies": {
        "python": [
            "boto3",
        ],
    },
    "data": [
        "security/etp_applicant_assessment_security.xml",
        "security/ir.model.access.csv",
        "data/ir_config_parameter.xml",
        "data/mail_template.xml",
        "data/cron.xml",
        "views/hr_job_views.xml",
        "views/hr_applicant_views.xml",
        "views/applicant_assessment_views.xml",
        "views/question_bank_views.xml",
        "views/assessment_template_views.xml",
        "views/portal_templates.xml",
        "views/menu_items.xml",
    ],
}
