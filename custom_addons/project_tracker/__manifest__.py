{
    "name": "Project Tracker",
    "version": "19.0.1.3.0",
    "category": "Tools",
    "summary": "Project Tracker — task allocation pipeline, team management and dashboards for every project",
    "description": """
        Project Tracker for Ethara ETP (technical name project_tracker).
        Extracted from the Kensei2 module: task allocation funnel (stage 1/2),
        stage hand-offs, bulk persona/allocation uploads, team management and
        the org/tasker/daily dashboards.
    """,
    "author": "Ethara",
    # kensei2         — the Tracker allocates kensei2.persona records. Access is
    #                   NOT tied to Kensei2's roles: the Tracker carries its own
    #                   group ladder (security/project_tracker_groups.xml).
    # mail            — project.tracker.allocation inherits mail.thread +
    #                   mail.activity.mixin (the chatter is the audit trail).
    # hr              — tasker/PL/QL resolve to hr.employee records.
    # etp_user_roles  — the Tracker's record rules bind to etp_user_roles.group_*
    #                   (tasker / quality_lead / project_lead / quality_reviewer /
    #                   cto / hr_admin / it_admin). Without it those refs cannot
    #                   resolve and the security XML fails to load.
    "depends": ["kensei2", "mail", "hr", "etp_user_roles"],
    "data": [
        "security/project_tracker_groups.xml",
        "security/project_tracker_security.xml",
        "security/ir.model.access.csv",
        "views/project_tracker_persona_import_views.xml",
        "views/project_tracker_bulk_allocation_views.xml",
        "views/project_tracker_persona_views.xml",
        "views/project_tracker_stage_handoff_views.xml",
        "views/project_tracker_views.xml",
        "views/project_tracker_team_import_views.xml",
        "views/project_tracker_team_views.xml",
        "views/project_tracker_project_views.xml",
        "views/menuitems.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": True,
    "license": "LGPL-3",
    "assets": {
        "web.assets_backend": [
            # tracker_common must precede anything importing @project_tracker/tracker_common.
            "project_tracker/static/src/tracker_common.js",
            "project_tracker/static/src/team_notification_service.js",
            "project_tracker/static/src/list_stats/list_stats.js",
            "project_tracker/static/src/list_stats/list_stats.xml",
            "project_tracker/static/src/list_stats/list_stats.scss",
            "project_tracker/static/src/dashboard_base/dashboard_base.js",
            "project_tracker/static/src/dashboard_base/dashboard_base.xml",
            "project_tracker/static/src/progress_table/progress_table.js",
            "project_tracker/static/src/progress_table/progress_table.xml",
            "project_tracker/static/src/statusbar/statusbar.js",
            "project_tracker/static/src/tracker_dashboard/tracker_dashboard.js",
            "project_tracker/static/src/tracker_dashboard/tracker_dashboard.xml",
            "project_tracker/static/src/tracker_dashboard/tracker_dashboard.scss",
            "project_tracker/static/src/tasker_dashboard/tasker_dashboard.js",
            "project_tracker/static/src/tasker_dashboard/tasker_dashboard.xml",
            "project_tracker/static/src/tasker_dashboard/tasker_dashboard.scss",
            "project_tracker/static/src/main_dashboard/main_dashboard.js",
            "project_tracker/static/src/tracker_daily/tracker_daily.js",
            "project_tracker/static/src/tracker_daily/tracker_daily.xml",
            "project_tracker/static/src/tracker_daily/tracker_daily.scss",
            "project_tracker/static/src/tracker_allocation.scss",
            "project_tracker/static/src/tracker_autosave/tracker_autosave.js",
            "project_tracker/static/src/tracker_stepper/tracker_stepper.js",
            "project_tracker/static/src/tracker_stepper/tracker_stepper.xml",
            "project_tracker/static/src/tracker_stepper/tracker_stepper.scss",
            "project_tracker/static/src/tracker_score_field/tracker_score_field.js",
            "project_tracker/static/src/tracker_drive_field/tracker_drive_field.js",
        ],
    },
}
