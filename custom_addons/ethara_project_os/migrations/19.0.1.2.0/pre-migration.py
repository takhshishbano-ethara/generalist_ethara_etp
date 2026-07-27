"""Move the Project OS project onto ``project.project`` (data model v2 §4.2, §12).

v1 of this module carried its own ``epo.project``; 19.0.1.1.0 folded that into
``ethara.project``; v2 reverses both and puts the project on Odoo's native
``project.project``. This migration performs the second move.

Only the rows this pipeline owns come across. ``ethara.project`` keeps every project the
budget subsystem created — that module, its budgets, phases and AWS costing are
untouched, and the rows it owns are not copied anywhere.

The order matters:

1. Add the Ethara columns to ``project_project`` by hand. The ORM would add them a
   moment later anyway, but the rows copied in step 2 have to land somewhere.
2. Copy the ``is_project_os`` rows across, keeping an id map. Everything else in
   ``ethara_project`` stays exactly where it is.
3. Resolve code collisions. Only the code is unique (§4.2); names are left alone,
   because Odoo's own project.project allows duplicates and the table has four owners.
4. Repoint the thirteen foreign keys.
5. Clear the Ethara columns off the ``ethara_project`` rows that moved, so the old
   registry does not keep a half-populated shadow of a project that now lives
   elsewhere.

Idempotent: re-running finds no unmigrated rows and returns immediately.
"""

import json
import logging

_logger = logging.getLogger(__name__)

DEPENDENTS = [
    ('epo_folder', 'project_id'),
    ('epo_document', 'project_id'),
    ('epo_training', 'project_id'),
    ('epo_assessment_link', 'project_id'),
    ('epo_assessment_result', 'project_id'),
    ('epo_form_template', 'project_id'),
    ('epo_form_entry', 'project_id'),
    ('epo_allocation', 'project_id'),
    ('epo_allocation_phase', 'project_id'),
    ('epo_onboarding', 'project_id'),
    ('epo_roster_day', 'project_id'),
    ('epo_timeline_event', 'project_id'),
    ('hr_employee', 'epo_current_project_id'),
]

# ethara_project column -> project_project column. Anything not listed keeps its name.
RENAMED = {
    'start_date': 'date_start',
    'end_date': 'date',
    'os_state': 'ethara_state',
    'project_type': 'ethara_project_type',
}

CARRIED = [
    'name', 'code', 'project_type', 'platform', 'description', 'os_state',
    'min_assessment_score', 'gpm_id', 'created_by_emp_id', 'start_date', 'end_date',
    'activated_at', 'archived_at', 'has_sop', 'has_stagelist',
    'has_training', 'has_assessment', 'active',
    'create_uid', 'create_date', 'write_uid', 'write_date',
]

NEW_COLUMNS = [
    ('is_project_os', 'boolean DEFAULT false'),
    ('ethara_state', "varchar DEFAULT 'setup'"),
    ('code', 'varchar'),
    ('ethara_project_type', "varchar DEFAULT 'external'"),
    ('platform', 'varchar'),
    ('min_assessment_score', 'double precision DEFAULT 0'),
    ('gpm_id', 'integer'),
    ('created_by_emp_id', 'integer'),
    ('activated_at', 'timestamp'),
    ('archived_at', 'timestamp'),
    ('has_sop', 'boolean DEFAULT false'),
    ('has_common_errors', 'boolean DEFAULT false'),
    ('has_task_videos', 'boolean DEFAULT false'),
    ('has_stagelist', 'boolean DEFAULT false'),
    ('has_feedback', 'boolean DEFAULT false'),
    ('has_training', 'boolean DEFAULT false'),
    ('has_assessment', 'boolean DEFAULT false'),
    ('gate_blockers', 'varchar'),
    ('assessment_link_id', 'integer'),
    ('stagelist_template_id', 'integer'),
    ('feedback_template_id', 'integer'),
]

STALE_XMLIDS = {
    'ir.ui.view': ('view_epo_project_list', 'view_epo_project_kanban',
                   'view_epo_project_form', 'view_epo_project_search',
                   'view_ethara_project_form_delivery',
                   'view_ethara_project_list_delivery',
                   'view_ethara_project_search_delivery'),
    'ir.actions.act_window': ('action_epo_project',),
    'ir.rule': ('rule_project_live_only', 'rule_project_manager'),
    'ir.model.access': ('access_epo_project_member', 'access_epo_project_manager',
                        'access_epo_project_admin'),
}


def _default_lang(cr):
    """The language key Odoo stores translated terms under."""
    cr.execute("SELECT code FROM res_lang WHERE active ORDER BY id LIMIT 1")
    row = cr.fetchone()
    return row[0] if row else 'en_US'


def _table_exists(cr, table):
    cr.execute('SELECT to_regclass(%s)', (f'public.{table}',))
    return cr.fetchone()[0] is not None


def _column_exists(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def _add_columns(cr):
    for column, ddl in NEW_COLUMNS:
        if not _column_exists(cr, 'project_project', column):
            cr.execute(f'ALTER TABLE project_project ADD COLUMN "{column}" {ddl}')
    # Rows the Project app and the other three modules own were never in this pipeline
    # and must not be dragged into its gate.
    cr.execute("""
        UPDATE project_project
           SET is_project_os = COALESCE(is_project_os, false),
               ethara_state   = COALESCE(ethara_state, 'setup'),
               has_sop        = COALESCE(has_sop, false),
               has_common_errors = COALESCE(has_common_errors, false),
               has_task_videos   = COALESCE(has_task_videos, false),
               has_stagelist  = COALESCE(has_stagelist, false),
               has_feedback   = COALESCE(has_feedback, false),
               has_training   = COALESCE(has_training, false),
               has_assessment = COALESCE(has_assessment, false),
               min_assessment_score = COALESCE(min_assessment_score, 0)
    """)


def _copy_projects(cr):
    """Insert one project_project row per Ethara ethara_project row; return the id map."""
    carried = [c for c in CARRIED if _column_exists(cr, 'ethara_project', c)]
    cr.execute(
        f'SELECT id, {", ".join(carried)} FROM ethara_project '
        f'WHERE is_project_os ORDER BY id')
    rows = cr.fetchall()
    if not rows:
        return {}

    # project.project requires company_id and privacy_visibility; neither exists on the
    # registry, so both are taken from the environment rather than invented per row.
    cr.execute('SELECT id FROM res_company ORDER BY id LIMIT 1')
    company = cr.fetchone()
    company_id = company[0] if company else None

    # `project.project.name` is translate=True, so its column is jsonb, not varchar —
    # a plain string is rejected outright ("invalid input syntax for type json"). The
    # source columns are plain text, so each one has to be wrapped in the term map
    # Odoo stores. Discovered by running the upgrade, not by reading the model.
    cr.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'project_project' AND data_type = 'jsonb'
    """)
    jsonb_columns = {r[0] for r in cr.fetchall()}
    lang = _default_lang(cr)

    # Everything project.project demands and the source has no answer for. Enumerated
    # from information_schema rather than guessed, because the native model gains
    # required fields between releases and each one is a separate 2am failure:
    #   alias_id            -> the mail alias, minted per row below
    #   name                -> jsonb, handled above
    #   privacy_visibility  -> Odoo's portal setting; 'employees' is the safe default
    #   last_update_status  -> the project-update state; 'to_define' means "nobody has
    #                          said yet", which is exactly true of a migrated project
    required = {'privacy_visibility': 'employees', 'last_update_status': 'to_define'}
    cr.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'project_project'
           AND is_nullable = 'NO' AND column_default IS NULL
    """)
    unhandled = {r[0] for r in cr.fetchall()} - set(required) - {'alias_id', 'name'}
    if unhandled:
        raise RuntimeError(
            'project.project has required columns this migration does not fill: %s. '
            'Add them to `required` in migrations/19.0.1.2.0/pre-migration.py.'
            % ', '.join(sorted(unhandled)))

    targets = [RENAMED.get(c, c) for c in carried]
    columns = ['is_project_os', 'company_id'] + list(required) + targets
    quoted = ', '.join(f'"{c}"' for c in columns)
    placeholders = ', '.join(['%s'] * len(columns))

    # project.project inherits mail.alias.mixin, so alias_id is NOT NULL — every
    # project owns a mail alias, normally minted by the mixin's create(). A migration
    # runs below the ORM, so the alias has to be created here or the insert is refused.
    cr.execute("SELECT id FROM ir_model WHERE model = 'project.task'")
    task_model = cr.fetchone()
    task_model_id = task_model[0] if task_model else None

    mapping = {}
    for row in rows:
        old_id, values = row[0], list(row[1:])
        for i, target in enumerate(targets):
            if target in jsonb_columns and values[i] is not None:
                values[i] = json.dumps({lang: values[i]})
        cr.execute("""
            INSERT INTO mail_alias (alias_model_id, alias_defaults, alias_contact,
                                    alias_status, create_uid, write_uid,
                                    create_date, write_date)
                 VALUES (%s, '{}', 'everyone', 'not_tested', 1, 1, now(), now())
              RETURNING id
        """, (task_model_id,))
        alias_id = cr.fetchone()[0]
        cr.execute(
            f'INSERT INTO project_project ("alias_id", {quoted}) '
            f'VALUES (%s, {placeholders}) RETURNING id',
            [alias_id, True, company_id] + list(required.values()) + values)
        new_id = cr.fetchone()[0]
        # The alias routes incoming mail to a task on this project; it can only be
        # pointed at the project once the project has an id.
        cr.execute("UPDATE mail_alias SET alias_defaults = %s WHERE id = %s",
                   ("{'project_id': %d}" % new_id, alias_id))
        mapping[old_id] = new_id

    _logger.info('Project OS upgrade: copied %s project(s) onto project.project.',
                 len(mapping))
    return mapping


def _dedupe(cr):
    """Break code collisions before the unique index is applied.

    Only Ethara rows carry a code, so this can only ever fire on two projects this
    pipeline owns.
    """
    # Only `code` — that is the one uniqueness rule the doc asks for (§4.2), so a
    # duplicate name is not a reason to rename anybody's project.
    for column, expr, where, suffix in (
            ('code', 'code', 'WHERE code IS NOT NULL', "code || '-' || id"),):
        cr.execute(f"""
            SELECT lower({expr}), array_agg(id ORDER BY id)
              FROM project_project {where} AND {expr} IS NOT NULL
          GROUP BY lower({expr}) HAVING count(*) > 1
        """)
        for key, ids in cr.fetchall():
            for project_id in ids[1:]:
                cr.execute(
                    f'UPDATE project_project SET {column} = {suffix} '
                    f'WHERE id = %s RETURNING {expr}', (project_id,))
                _logger.error(
                    'Project OS upgrade: project.project id=%s shared the %s %r with '
                    'id=%s and was renamed to %r so the new uniqueness rule could be '
                    'applied. Review it.',
                    project_id, column, key, ids[0], cr.fetchone()[0])


def _repoint(cr, mapping):
    if not mapping:
        return
    old_ids = list(mapping)
    new_ids = [mapping[i] for i in old_ids]
    for table, column in DEPENDENTS:
        if not _table_exists(cr, table) or not _column_exists(cr, table, column):
            continue
        cr.execute("""
            SELECT conname FROM pg_constraint
             WHERE conrelid = %s::regclass AND contype = 'f'
               AND confrelid = 'ethara_project'::regclass
        """, (table,))
        for (constraint,) in cr.fetchall():
            cr.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "{constraint}"')
        cr.execute(f"""
            UPDATE "{table}" t
               SET "{column}" = m.new_id
              FROM (SELECT unnest(%s::int[]) AS old_id,
                           unnest(%s::int[]) AS new_id) m
             WHERE t."{column}" = m.old_id
        """, (old_ids, new_ids))
        _logger.info('Project OS upgrade: repointed %s.%s (%s row(s)).',
                     table, column, cr.rowcount)


def _demote_registry_rows(cr, mapping):
    """Clear the pipeline columns off the ethara_project rows that moved.

    The rows themselves stay — the budget subsystem may reference them — but they are
    no longer Project OS projects, and leaving is_project_os set would give the
    database two records each claiming to be the same live project.
    """
    if not mapping:
        return
    cr.execute("""
        UPDATE ethara_project
           SET is_project_os = false, os_state = 'setup',
               has_sop = false, has_stagelist = false
         WHERE id = ANY(%s)
    """, (list(mapping),))
    _logger.info('Project OS upgrade: demoted %s ethara.project row(s); the budget '
                 'side keeps them, this pipeline no longer claims them.', cr.rowcount)


def _drop_stale_ui(cr):
    for model, names in STALE_XMLIDS.items():
        cr.execute("""
            SELECT res_id FROM ir_model_data
             WHERE module = 'ethara_project_os' AND model = %s AND name IN %s
        """, (model, tuple(names)))
        res_ids = tuple(r[0] for r in cr.fetchall())
        if not res_ids:
            continue
        table = 'ir_act_window' if model == 'ir.actions.act_window' else model.replace('.', '_')
        cr.execute(f'DELETE FROM "{table}" WHERE id IN %s', (res_ids,))
        cr.execute("""
            DELETE FROM ir_model_data
             WHERE module = 'ethara_project_os' AND model = %s AND name IN %s
        """, (model, tuple(names)))
    # The reporting view joins the project table; post_init_hook recreates it.
    cr.execute('DROP VIEW IF EXISTS epo_v_submission_count')


def migrate(cr, version):
    if not version:
        return
    if not _table_exists(cr, 'project_project'):
        raise RuntimeError(
            'The `project` module must be installed before ethara_project_os '
            '19.0.1.2.0 — the project entity is now project.project.')
    if not _table_exists(cr, 'ethara_project') or not _column_exists(
            cr, 'ethara_project', 'is_project_os'):
        _logger.info('Project OS upgrade: nothing to move from ethara.project.')
        _drop_stale_ui(cr)
        return

    _add_columns(cr)
    mapping = _copy_projects(cr)
    _dedupe(cr)
    _repoint(cr, mapping)
    _demote_registry_rows(cr, mapping)
    _drop_stale_ui(cr)
    _logger.info('Project OS upgrade: the project entity is now project.project.')
