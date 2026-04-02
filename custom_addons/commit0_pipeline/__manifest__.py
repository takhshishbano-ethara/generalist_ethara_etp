# -*- coding: utf-8 -*-
{
    "name": "Commit0 Pipeline",
    "version": "18.0.1.0.0",
    "category": "Productivity",
    "summary": "Orchestration UI for commit0 AI coding agent trajectory pipeline",
    "description": """
Commit0 Pipeline
=================
Orchestration interface for the commit0 AI coding agent trajectory pipeline.

Features:
- Discover Python repositories from GitHub by star count and criteria
- Validate, fork, stub, and prepare repositories for AI agent evaluation
- Generate datasets, test IDs, and Docker environments
- Track pipeline progress with real-time state updates
- Batch processing via CSV import
    """,
    "author": "GRT Labs",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": [
        "security/commit0_security.xml",
        "security/ir.model.access.csv",
        "data/commit0_data.xml",
        "views/pipeline_run_views.xml",
        "views/repo_entry_views.xml",
        "views/discovery_candidate_views.xml",
        "views/repo_evaluation_views.xml",
        "views/res_config_settings_views.xml",
        "views/commit0_menus.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
    "assets": {
        "web.assets_backend": [
            "commit0_pipeline/static/src/components/repo_browser/repo_browser.js",
            "commit0_pipeline/static/src/components/repo_browser/repo_browser.xml",
            "commit0_pipeline/static/src/components/repo_browser/repo_browser.scss",
        ],
    },
}
