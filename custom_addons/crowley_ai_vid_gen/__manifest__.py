# -*- coding: utf-8 -*-
{
    "name": "Crowley AI Video Generation",
    "version": "19.0.1.0.0",
    "summary": "Bit-perfect AI video generation via OpenRouter Seedance 2.0 with S3 storage and in-Odoo playback.",
    "description": """
Crowley AI Video Generation
===========================

Generate videos via ByteDance Seedance 2.0 (through OpenRouter), store them on
S3 with no re-encoding, and play them back in Odoo using the ``video_preview``
widget shipped by ``instagram_video_qc_manager``.

**Bit-Perfect (No Re-Encode) Pipeline**: the MP4 returned by OpenRouter is
uploaded to S3 byte-for-byte (SHA-256 verified) and served to the browser
unchanged. HTML5 ``<video>`` decodes the original H.264 stream directly.

Features
--------

* Text-to-video generation (Seedance 2.0 / Seedance 2.0 Fast)
* Async job lifecycle: submit, poll, download, store, play
* OpenRouter webhook handler (HMAC-SHA256 verified, idempotent)
* ``ir.cron`` polling fallback (30s interval, automatic)
* Per-job SHA-256 verification of the bytes from OpenRouter -> S3
* Presigned S3 GET URLs with configurable TTL (default 5 minutes)
* In-Odoo HTML5 playback with browser-native HTTP-Range seeking
* Authenticated download via signed S3 redirect (``Content-Disposition: attachment``)
* Role-based security: User / Creator / Manager with record rules
* Backend-only (``auth="user"``); no public exposure
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
        "python": ["requests", "boto3", "botocore"],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_sequence.xml",
        "data/ir_config_param.xml",
        "data/ir_cron.xml",
        "views/res_config_settings_views.xml",
        "views/crowley_ai_vid_gen_job_views.xml",
        "views/crowley_ai_vid_gen_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "crowley_ai_vid_gen/static/src/fields/video_preview/video_preview_field.js",
            "crowley_ai_vid_gen/static/src/fields/video_preview/video_preview_field.xml",
            "crowley_ai_vid_gen/static/src/studio/studio.scss",
            "crowley_ai_vid_gen/static/src/studio/studio.js",
            "crowley_ai_vid_gen/static/src/studio/studio.xml",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
    "post_init_hook": "post_init_hook",
}
