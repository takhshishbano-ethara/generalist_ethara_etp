{
    "name": "Aurora Extension",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "REST API for the Aurora / Milo-Bench showcase dashboard.",
    "description": """
        Read-only REST API that powers the Flutter Aurora (Milo-Bench)
        showcase dashboard.

        Real data (computed from aurora.evaluation.instance):
          - KPIs (instances, resolved, resolve rate, repos covered)
          - Quality tiers (status distribution)
          - Paginated benchmark instances table

        Seeded / admin-editable content (aurora.showcase.* models):
          - Methodology principles
          - Pipeline steps
          - External resources (GitHub / HuggingFace / Paper)
          - Per-model summary stats (Kimi / GLM-5)

        All responses follow the api_auth_gateway return_Response envelope
        and are guarded by @validate_token, matching talos_extension /
        crowley_extension.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "aurora",
        "api_auth_gateway",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/aurora_showcase_data.xml",
    ],
    "installable": True,
    "application": False,
}
