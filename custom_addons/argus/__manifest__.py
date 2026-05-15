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
        # task_forge_core pulls in task_forge_bridge transitively,
        # which is where the hr.employee role hierarchy fields live
        # (``task_forge_pl_id`` / ``task_forge_qr_id`` / ``task_forge_active``).
        # Argus reuses that hierarchy on new tasks: the Tasker picks
        # an Employee on the form, and the PL / QL slots auto-fill
        # from the employee's PL / QR pointers.  Listing
        # task_forge_core here means installing Argus pulls in
        # core + bridge + their transitive deps in one go.
        "task_forge_core",
        "api_auth_gateway",
    ],
    "external_dependencies": {
        "python": ["requests", "yt_dlp", "instaloader"],
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
