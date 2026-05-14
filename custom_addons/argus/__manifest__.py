# -*- coding: utf-8 -*-
{
    "name": "Argus",
    "version": "19.0.1.0.0",
    "summary": "Instagram video comparison & review workflow (PL / QL / final decision).",
    "description": """
Argus
=====

Video task management module for AI-generated Instagram content
review.  Each task carries an Input Video URL (original reel) and an
Output Video URL (AI-generated rendition), plus a prompt, a Project
Lead, a Quality Lead, an email, and a full QC + final-decision
workflow.

Features
--------
* Two-URL Instagram task records with strict regex validation
* Project Lead / Quality Lead assignment + record rules
* QC workflow (pending / approved / rejected / needs revision)
* Final decision tracking distinct from per-stage QC verdict
* Duplicate detection via shortcode pairs (input + output)
* Kanban / list / pivot / graph reports out of the box
* Built-in CSV / Excel export (no extra dependency)
* Mail thread + activities — audit trail in chatter
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
        # ``requests`` powers the Bedrock Converse call in
        # ``services/grammar_checker.py``.  Argus is standalone and
        # owns its own copy of the call (does NOT import from
        # task_forge_core) so the operator can configure the two
        # modules with separate API keys.
        #
        # NOTE: ``yt-dlp`` is a *soft* dependency, not listed here.
        # Listing it would make Odoo refuse to load the module when
        # it's missing.  Instead the preview controller imports it
        # behind a try/except and falls back to regex scraping +
        # finally to Instagram's iframe.  For real in-popup playback
        # install it manually in the Odoo Python environment:
        #     pip install yt-dlp
        "python": ["requests"],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/sequences.xml",
        "views/argus_task_views.xml",
        "views/argus_dashboard_views.xml",
        "views/argus_video_preview_wizard_views.xml",
        "views/res_config_settings_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
