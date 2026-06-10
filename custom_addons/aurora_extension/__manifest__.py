{
    "name": "Aurora Extension",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "REST API for the Aurora / Milo-Bench showcase dashboard.",
    "description": """
        Read-only REST API that powers the Flutter Aurora (Milo-Bench)
        project dashboard, computed live from aurora.evaluation.instance:
          - Overview: KPIs, stage funnel, recent activity
          - Analytics: resolve-rate KPIs, resolution mix, instances/day
          - Instances: paginated, filterable benchmark instances table

        Mirrors the crowley_sourcing_extension dashboard contract so the same
        Flutter project-detail machinery renders Aurora via an auto-created
        internal project (data/aurora_project_data.xml). All responses follow
        the api_auth_gateway return_Response envelope and are guarded by
        @validate_token.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "aurora",
        "api_auth_gateway",
        # Provides project.project's project_classification / connected_table /
        # api_map_ids fields used by the auto-created Aurora external project.
        "etp_projects",
    ],
    "data": [
        "data/aurora_project_data.xml",
    ],
    "installable": True,
    "application": False,
}
