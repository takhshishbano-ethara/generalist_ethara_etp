# -*- coding: utf-8 -*-
{
    "name": "Instagram Video QC Manager",
    "version": "19.0.1.0.0",
    "summary": "Download, edit, trim and run QC on Instagram videos with full versioning.",
    "description": """
Instagram Video QC Manager
==========================

Production-grade Odoo 19 module for ingesting Instagram reels/videos, performing
non-destructive editing (trim/crop/filters) via FFmpeg, capturing AI/content
prompts and running an iterative QC workflow with full version history.

Highlights
----------
* Instagram reel / post downloader (yt-dlp) with thumbnail capture
* Non-blocking FFmpeg processing pipeline (after-commit hook)
* OWL-based fullscreen video editor with trim / crop / filter timeline
* Per-version prompt + QC review (approve / reject / rework)
* Unlimited edit-QC cycles in a single task with full history
* Smart-button dashboard, kanban boards, QC queue
* Security: User / Editor / QC Reviewer / Manager roles
""",
    "author": "Ethara",
    "website": "https://ethara.example.com",
    "category": "Marketing/Social Marketing",
    "license": "LGPL-3",
    "depends": [
        "base",
        "mail",
        "web",
    ],
    "external_dependencies": {
        "python": ["instaloader", "requests"],
        "bin": ["ffmpeg", "ffprobe"],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/sequences.xml",
        "data/mail_templates.xml",
        "wizard/video_qc_review_wizard_views.xml",
        "wizard/video_prompt_wizard_views.xml",
        "views/video_task_views.xml",
        "views/video_version_views.xml",
        "views/video_edit_history_views.xml",
        "views/video_processing_log_views.xml",
        "views/video_qc_views.xml",
        "views/video_editor_action.xml",
        "views/video_dashboard_views.xml",
        "views/menus.xml",
    ],
    "demo": [
        "data/demo.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "instagram_video_qc_manager/static/src/scss/editor.scss",
            "instagram_video_qc_manager/static/src/scss/dashboard.scss",
            "instagram_video_qc_manager/static/src/js/services/**/*.js",
            "instagram_video_qc_manager/static/src/js/widgets/**/*.js",
            "instagram_video_qc_manager/static/src/js/widgets/**/*.xml",
            "instagram_video_qc_manager/static/src/js/video_editor/**/*.js",
            "instagram_video_qc_manager/static/src/js/video_editor/**/*.xml",
        ],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": True,
    "auto_install": False,
}
