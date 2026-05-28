{
    "name": "WildClaw Core",
    "version": "19.0.1.0.0",
    "category": "Productivity/AI",
    "summary": "Shared core for WildClawBench-powered agent sandbox modules (kensei/skoll/talos).",
    "description": """
WildClaw Core
=============

Shared base addon providing:

* Vendored WildClawBench Python library (OpenClaw harness only) — agent execution engine.
* Abstract task & sandbox models (wildclaw.task_base, wildclaw.sandbox_base) inherited by wrappers.
* Persona, domain, api_request, test_result models shared by all wrappers.
* Common controllers: browser_auth, gog_auth, trajectory_qc_validator, llm_assist_qc, media_upload.
* Shared services: WildClawBench bridge runner, RabbitMQ consumer, OpenClaw WS client.
* Multimedia subsystem: image/video upload, video frame extraction, PDF/document analysis,
  inline-base64-to-S3 media replacement.
* Sandbox container artifacts (sandbox_docker/) and Kubernetes manifests (local-k8s/).
* Shared OWL widgets: chat, sandbox_iframe, sandbox_card, dashboards, media_preview,
  markdown_field, json_field, gog_auth_dialog.

Wrappers built on this core (in this repository):

* kensei_wildclaw — kensei2 features (file attachments + intent_test_generation + SSE).
* skoll_wildclaw — skoll_project features (golden_generation pipeline + 4 tag models +
  skoll_generation cost model).
* talos_wildclaw — talos features (auto-hint loop + export controller).
""",
    "author": "Ethara Labs",
    "website": "https://ethara.ai",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "hr",
        "bus",
        "mail",
    ],
    "external_dependencies": {
        "python": [
            "boto3",
            "websockets",
            "httpx",
            "pika",
            "pyyaml",
            "Pillow",
            "PyPDF2",
            "yt-dlp",
            "huggingface-hub",
        ],
        # bin: ffmpeg & ffprobe required at runtime (installed via Homebrew)
        # but omitted here to avoid install-time PATH mismatch.
    },
    "data": [
        "security/wildclaw_security.xml",
        "security/ir.model.access.csv",
        "data/persona_seed.xml",
        "data/domain_seed.xml",
        "views/wildclaw_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": True,
}
