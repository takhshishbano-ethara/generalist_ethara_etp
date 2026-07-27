"""Install hooks.

**pre_init** creates the ``btree_gist`` extension. It has to exist *before* the ORM
applies table constraints, because three models declare EXCLUDE constraints — the only
way to forbid overlapping date ranges atomically. A Python ``@api.constrains`` check
reads, decides, then writes; two concurrent requests both read "no overlap" and both
write. At roster scale that race is not theoretical.

**post_init** adds what the ORM still cannot express:

* covering and partial indexes on the hot read paths;
* two reporting views the analytics endpoints read instead of aggregating in Python.

Every statement is idempotent — the hook runs on install and on every upgrade.
"""

import logging

_logger = logging.getLogger(__name__)


def pre_init_hook(env):
    """btree_gist gives EXCLUDE the ability to mix `=` on a scalar with `&&` on a range."""
    env.cr.execute('CREATE EXTENSION IF NOT EXISTS btree_gist')
    _logger.info('Project OS: btree_gist ready.')


DDL = [
    # Hot-path indexes. The composite on (employee, date) covers the six lookups that
    # resolve "this person, this day". Deliberately NOT a partial index on
    # CURRENT_DATE, which would need rebuilding every midnight.
    """
    CREATE INDEX IF NOT EXISTS epo_roster_lookup_idx
        ON epo_roster_day (employee_id, business_date DESC)
        INCLUDE (project_id, tasking_status, present)
    """,
    """
    CREATE INDEX IF NOT EXISTS epo_entry_count_idx
        ON epo_form_entry (project_id, form_type, business_date)
        WHERE state = 'submitted'
    """,
    """
    CREATE INDEX IF NOT EXISTS epo_entry_scope_idx
        ON epo_form_entry (employee_id, form_type, submitted_at DESC)
        WHERE state = 'submitted'
    """,
    """
    CREATE INDEX IF NOT EXISTS epo_timeline_employee_idx
        ON epo_timeline_event (employee_id, occurred_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS epo_timeline_project_idx
        ON epo_timeline_event (project_id, occurred_at DESC)
    """,

    # Reporting views. The single ground truth for every count on every screen: there
    # are no counter columns anywhere in this module, so nothing can drift.
    """
    CREATE OR REPLACE VIEW epo_v_submission_count AS
        SELECT e.project_id, p.name AS project_name, e.form_type, e.employee_id,
               e.pod_id, e.business_date, count(*)::bigint AS submissions
          FROM epo_form_entry e
          JOIN project_project p ON p.id = e.project_id
         WHERE e.state = 'submitted'
         GROUP BY e.project_id, p.name, e.form_type, e.employee_id, e.pod_id,
                  e.business_date
    """,
    # Time spent per phase, per person, per project — the answer to "how much of this
    # project went into getting people ready?" without scanning the roster.
    """
    CREATE OR REPLACE VIEW epo_v_phase_summary AS
        SELECT ph.project_id, ph.employee_id, ph.phase,
               min(ph.date_from) AS first_day,
               max(COALESCE(ph.date_to, CURRENT_DATE)) AS last_day,
               sum(ph.duration_days)::bigint AS days,
               count(*)::bigint AS stretches
          FROM epo_allocation_phase ph
         GROUP BY ph.project_id, ph.employee_id, ph.phase
    """,
]


def post_init_hook(env):
    cr = env.cr
    for statement in DDL:
        try:
            cr.execute(statement)
        except Exception as exc:  # noqa: BLE001
            # Log loudly and continue: a missing index degrades performance, it does not
            # break correctness, and an install that dies here is worse.
            cr.rollback()
            _logger.error(
                'Project OS: could not apply DDL, continuing without it.\n%s\n%s',
                statement.strip().splitlines()[0], exc)
        else:
            cr.commit()
    _logger.info('Project OS: post-install DDL applied.')
