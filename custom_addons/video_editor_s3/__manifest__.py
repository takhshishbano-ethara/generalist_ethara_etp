# -*- coding: utf-8 -*-
{
    "name": "Crowley Sourcing",
    "version": "19.0.1.0.5",
    "summary": "Import S3 videos, edit (trim/crop/rotate/resize/filters) via FFmpeg, "
               "render asynchronously and export back to S3.",
    "description": """
Video Editor S3
===============

Production-grade Odoo 19 module that lets editors:

* import a video by S3 URL (no Instagram dependency),
* preview / scrub / trim / crop / rotate / resize / mute / colour-grade
  in an OWL-based fullscreen editor,
* render server-side via FFmpeg in a thread-pool executor with
  cooperative cancellation, heartbeats and per-job logs,
* export the final render back to S3 with multipart upload, retry and
  bandwidth logging.

Sibling to ``instagram_video_qc_manager``; the two modules are
intentionally independent so each can evolve on its own schedule.
""",
    "author": "Ethara",
    "website": "https://ethara.example.com",
    "category": "Productivity/Video",
    "license": "LGPL-3",
    "depends": [
        "base",
        "mail",
        "web",
    ],
    "external_dependencies": {
        "python": ["boto3", "requests", "yt_dlp"],
        "bin": ["ffmpeg", "ffprobe"],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/sequences.xml",
        "data/cron.xml",
        "views/res_config_settings_views.xml",
        "views/video_editor_project_views.xml",
        "views/video_editor_job_views.xml",
        "views/video_editor_action.xml",
        "views/menus.xml",
    ],
    "post_init_hook": "post_init_hook",
    "assets": {
        "web.assets_backend": [
            "video_editor_s3/static/src/scss/editor.scss",
            "video_editor_s3/static/src/js/services/**/*.js",
            "video_editor_s3/static/src/js/fields/**/*.js",
            "video_editor_s3/static/src/js/fields/**/*.xml",
            "video_editor_s3/static/src/js/video_editor/**/*.js",
            "video_editor_s3/static/src/js/video_editor/**/*.xml",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
