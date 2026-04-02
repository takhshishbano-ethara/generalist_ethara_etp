# -*- coding: utf-8 -*-
{
    "name": "Commit0 Pipeline",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Kaiju Pipeline — 6-stage repository evaluation wizard for commit0",
    "description": """
Kaiju Pipeline
==============
Human-in-the-loop repository evaluation pipeline for commit0.

Features:
- 6-stage progressive wizard for repository evaluation
- Repository file browsing and understanding
- 9-point quality checklist with pass/fail gates
- Automated preparation: fork, reference commit, spec document generation
- Spec document review with PDF/JSON/YAML viewing
- Side-by-side stub code comparison and review
- Docker image generation and ECR deployment
    """,
    "author": "GRT Labs",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": [
        "security/commit0_security.xml",
        "security/ir.model.access.csv",
        "data/commit0_data.xml",
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
            "commit0_pipeline/static/src/components/auto_refresh/auto_refresh.js",
            "commit0_pipeline/static/src/components/auto_refresh/auto_refresh.xml",
        ],
    },
}
