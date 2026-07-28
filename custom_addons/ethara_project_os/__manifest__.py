{
    "name": "Ethara Project OS",
    "version": "19.0.1.5.1",
    "category": "Operations",
    "summary": "Project lifecycle OS: kickoff → SOP/training/assessment → stagelist/feedback → allocation → tasking",
    "description": """
Ethara Project OS
=================

The operating system for the project side of the pod organisation — the backend of
the `ethara-project-os` prototype, rebuilt on Odoo.

Four roles (login-scoped)
-------------------------
* **Admin** — full control. The only role that may void a submission, unlock a
  payroll-locked roster day, waive onboarding or read the audit log.
* **PM** (General Program Management) — owns projects: creates them, fills the
  knowledge folder (SOP / Common Errors / Task Videos), sets training, links the
  externally-built assessment, builds and publishes the stagelist + feedback forms,
  allocates people, reads org-wide analytics.
* **PL** (Pod Lead) — owns the daily roster of their pod: status, project, presence;
  approves their pod's leave; reviews their pod's submissions.
* **Tasker** (Tasker) — completes onboarding (SOP → Training → Assessment), then fills
  the stagelist and the feedback form for the project they are allocated to.

What it stores
--------------
* Project definition and the kickoff gate — a project cannot go live without an SOP
  and a published stagelist.
* Knowledge folder, training sessions, and a **federated** assessment link. The
  assessment is *built in another system* and pulled over an API; only the link, a
  redacted snapshot and the local result live here, so a remote outage never blocks
  a person who has already been graded.
* One form engine driving both the stagelist and the feedback taker, with
  published-template immutability and an append-only submission ledger.
* Who is assigned to which project, from when to when, at what percentage — and
  inside that, **how long each person spent in every phase**: onboarding, SOP,
  training, assessment, ramp-up, tasking, ramp-down.
* A unified append-only timeline. Read it by employee for a person's full history;
  read it by project for the project's full history.
""",
    "author": "Ethara",
    "website": "https://ethara.ai",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "mail",
        "hr",
        "hr_holidays",
        "hr_attendance",
        "api_auth_gateway",
        "s3_connector",
        # Data model v2 §1: the project entity IS project.project. Project OS keeps no
        # project table of its own — it extends the native model (models/project_project.py)
        # and scopes everything it owns with is_project_os, because three other modules
        # own rows on that table too.
        "project",
        # We CALL the assessment app, we never modify it: the project pushes its SOP and
        # training into an etp.assessment.prompt and reads back what results. Extracting
        # skills, choosing them and generating questions all happen over there.
        "etp_assessment",
    ],
    # No UI for the four roles: that is a separate frontend talking to
    # /api/project-os/*. What stays here is the operations surface Odoo is actually
    # good for — where the S3 bucket and the assessment API get configured, and a
    # read-only window on the audit log and the timeline for forensics.
    "data": [
        "security/epo_groups.xml",
        "security/ir.model.access.csv",
        "security/epo_rules.xml",
        "data/epo_sequence.xml",
        "data/epo_params.xml",
        "data/epo_cron.xml",
        "data/epo_mail_templates.xml",
        "views/project_project_views.xml",
        "views/epo_knowledge_views.xml",
        "views/epo_form_views.xml",
        "views/epo_allocation_views.xml",
        "views/epo_resource_views.xml",
        "views/epo_history_views.xml",
        "views/epo_config_views.xml",
        "views/epo_menus.xml",
    ],
    # Loaded only when the database is created with demo data. `--without-demo=all`
    # gives a completely empty system; deleting the demo/ folder and this key removes
    # the dummy data for good.
    "demo": [
        "demo/epo_demo.xml",
    ],
    "installable": True,
    "application": True,
    # btree_gist must exist before the ORM applies the EXCLUDE constraints.
    "pre_init_hook": "pre_init_hook",
    "post_init_hook": "post_init_hook",
}
