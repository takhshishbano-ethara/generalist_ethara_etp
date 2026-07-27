"""Fold ``epo.project`` into ``ethara.project``.

Version 1.1.0 removes this module's own project table. There is one project registry
in the database — ``ethara.project``, owned by ``ethara_project`` — and the Project OS
lifecycle is now a set of columns on it rather than a second table that had to be kept
in step with the first by hand.

This runs *before* the ORM loads the new model, which is the only window in which the
old table still exists and the new columns do not yet carry their constraints. The
order matters:

1. Add the Project OS columns to ``ethara_project`` by hand. The ORM would add them a
   moment later anyway, but the rows copied in step 2 have to land somewhere.
2. Copy every ``epo_project`` row into ``ethara_project``, keeping an id map.
   ``is_project_os`` is true on exactly those rows and false on everything that was
   already in the registry, which is what keeps the go-live gate off projects that
   were never in this pipeline.
3. Resolve name collisions. ``ethara.project`` never had a uniqueness rule, so a live
   database can hold two projects called "Atlas" — and a copied project can collide
   with a registry one. The new unique index would abort the whole upgrade on those,
   so they are renamed here and every rename is logged at ERROR level: a renamed
   project is recoverable from the log, an upgrade that dies at 2am is not.
4. Repoint the thirteen foreign keys that referenced ``epo_project``.
5. Drop the old table, its views and its metadata rows, so the registry does not try
   to resurrect a model whose Python class no longer exists.

Idempotent: re-running finds no ``epo_project`` table and returns immediately.
"""

import logging

_logger = logging.getLogger(__name__)

# Every stored column that pointed at epo_project.id.
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

# Columns carried across, and what they are called on the other side. The old `state`
# becomes `os_state`: the registry's own `state` is the commercial lifecycle and is
# set separately below.
RENAMED = {'date_start': 'start_date', 'date_end': 'end_date', 'state': 'os_state'}

CARRIED = [
    'name', 'code', 'project_type', 'platform', 'description', 'state',
    'min_assessment_score', 'gpm_id', 'date_start', 'date_end',
    'activated_at', 'archived_at', 'has_sop', 'has_stagelist',
    'has_training', 'has_assessment', 'active',
    'create_uid', 'create_date', 'write_uid', 'write_date',
]

NEW_COLUMNS = [
    ('is_project_os', 'boolean DEFAULT false'),
    ('os_state', "varchar DEFAULT 'setup'"),
    ('code', 'varchar'),
    ('project_type', "varchar DEFAULT 'external'"),
    ('platform', 'varchar'),
    ('description', 'text'),
    ('min_assessment_score', 'double precision DEFAULT 0'),
    ('gpm_id', 'integer'),
    ('created_by_emp_id', 'integer'),
    ('activated_at', 'timestamp'),
    ('archived_at', 'timestamp'),
    ('has_sop', 'boolean DEFAULT false'),
    ('has_stagelist', 'boolean DEFAULT false'),
    ('has_training', 'boolean DEFAULT false'),
    ('has_assessment', 'boolean DEFAULT false'),
    ('gate_blockers', 'varchar'),
    ('assessment_link_id', 'integer'),
    ('stagelist_template_id', 'integer'),
    ('feedback_template_id', 'integer'),
]

# UI records that named the old model. Removed rather than updated so the data files
# recreate them cleanly — an ir.ui.view left pointing at a dead model fails arch
# validation halfway through the upgrade, which is a much worse error to read.
STALE_XMLIDS = {
    'ir.ui.view': ('view_epo_project_list', 'view_epo_project_kanban',
                   'view_epo_project_form', 'view_epo_project_search'),
    'ir.actions.act_window': ('action_epo_project',),
    'ir.rule': ('rule_project_live_only', 'rule_project_manager'),
    'ir.model.access': ('access_epo_project_member', 'access_epo_project_manager',
                        'access_epo_project_admin'),
}


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
        if not _column_exists(cr, 'ethara_project', column):
            cr.execute(f'ALTER TABLE ethara_project ADD COLUMN "{column}" {ddl}')
    # Rows that were already in the registry were never in this pipeline, and must not
    # be dragged into its gate: not-a-Project-OS-project, in setup, gate flags clear.
    cr.execute("""
        UPDATE ethara_project
           SET is_project_os = COALESCE(is_project_os, false),
               os_state       = COALESCE(os_state, 'setup'),
               has_sop        = COALESCE(has_sop, false),
               has_stagelist  = COALESCE(has_stagelist, false),
               has_training   = COALESCE(has_training, false),
               has_assessment = COALESCE(has_assessment, false),
               min_assessment_score = COALESCE(min_assessment_score, 0)
    """)


def _copy_projects(cr):
    """Insert one ethara_project row per epo_project row; return {old_id: new_id}."""
    carried = [c for c in CARRIED if _column_exists(cr, 'epo_project', c)]
    cr.execute(f'SELECT id, {", ".join(carried)} FROM epo_project ORDER BY id')
    rows = cr.fetchall()
    if not rows:
        return {}

    # is_project_os marks the pipeline. client_name and state are required by the
    # registry and the Project OS kickoff never asked for either, so both are derived.
    columns = ['is_project_os', 'client_name', 'state'] + [
        RENAMED.get(c, c) for c in carried]
    quoted = ', '.join(f'"{c}"' for c in columns)
    placeholders = ', '.join(['%s'] * len(columns))

    mapping = {}
    for row in rows:
        old_id, values = row[0], list(row[1:])
        by_name = dict(zip(carried, values))
        # The platform the work lands on IS the client for external work, and says so
        # plainly for internal work.
        client = by_name.get('platform') or (
            'Ethara (internal)' if by_name.get('project_type') == 'internal'
            else by_name.get('name'))
        # Commercial state is the registry's own lifecycle and nobody has made a
        # commercial decision about these projects yet. 'start' is its default and the
        # only honest answer; the budget side sets it deliberately from here on.
        cr.execute(
            f'INSERT INTO ethara_project ({quoted}) VALUES ({placeholders}) RETURNING id',
            [True, client, 'start'] + values)
        mapping[old_id] = cr.fetchone()[0]

    _logger.info('Project OS upgrade: copied %s project(s) into ethara.project.',
                 len(mapping))
    return mapping


def _dedupe(cr):
    """Break name and code collisions before the new unique indexes are applied.

    Runs after the copy so it also catches a Project OS project colliding with a
    registry project that happened to share its name.
    """
    for column, suffix in (('name', " || ' (#' || id || ')'"), ('code', " || '-' || id")):
        cr.execute(f"""
            SELECT lower({column}), array_agg(id ORDER BY is_project_os, id)
              FROM ethara_project
             WHERE {column} IS NOT NULL
          GROUP BY lower({column})
            HAVING count(*) > 1
        """)
        # Ordered so a registry row keeps the original value and the copied Project OS
        # row is the one that moves — the GPM who renames it is the one who can.
        for key, ids in cr.fetchall():
            for project_id in ids[1:]:
                cr.execute(
                    f'UPDATE ethara_project SET {column} = {column}{suffix} '
                    f'WHERE id = %s RETURNING {column}', (project_id,))
                _logger.error(
                    'Project OS upgrade: ethara.project id=%s shared the %s %r with '
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
        # The existing FK still points at epo_project, where the new ids mean nothing.
        cr.execute("""
            SELECT conname FROM pg_constraint
             WHERE conrelid = %s::regclass AND contype = 'f'
               AND confrelid = 'epo_project'::regclass
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


def _drop_old_model(cr):
    # Recreated against ethara_project by post_init_hook.
    cr.execute('DROP VIEW IF EXISTS epo_v_submission_count')

    for model, names in STALE_XMLIDS.items():
        cr.execute("""
            SELECT res_id FROM ir_model_data
             WHERE module = 'ethara_project_os' AND model = %s AND name IN %s
        """, (model, tuple(names)))
        res_ids = tuple(r[0] for r in cr.fetchall())
        if not res_ids:
            continue
        table = model.replace('.', '_')
        if model == 'ir.actions.act_window':
            table = 'ir_act_window'
        cr.execute(f'DELETE FROM "{table}" WHERE id IN %s', (res_ids,))
        cr.execute("""
            DELETE FROM ir_model_data
             WHERE module = 'ethara_project_os' AND model = %s AND name IN %s
        """, (model, tuple(names)))

    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'ethara_project_os'
           AND (  (model = 'ir.model' AND name = 'model_epo_project')
               OR (model = 'ir.model.fields' AND name LIKE 'field_epo_project__%%'))
    """)
    # ir_model_fields, ir_model_access and ir_rule all cascade from ir_model.
    cr.execute("DELETE FROM ir_model WHERE model = 'epo.project'")
    cr.execute('DROP TABLE IF EXISTS epo_project CASCADE')
    _logger.info('Project OS upgrade: epo.project removed.')


def migrate(cr, version):
    if not version:
        return
    if not _table_exists(cr, 'epo_project'):
        _logger.info('Project OS upgrade: epo.project is already gone, nothing to do.')
        return
    if not _table_exists(cr, 'ethara_project'):
        # ethara_project is a hard dependency now, so the registry table is created
        # before this runs. If it is not there, something is wrong enough that
        # silently inventing a project table would make it worse.
        raise RuntimeError(
            'ethara_project must be installed before ethara_project_os 19.0.1.1.0 — '
            'the Project OS project is now a set of columns on ethara.project.')

    _add_columns(cr)
    mapping = _copy_projects(cr)
    _dedupe(cr)
    _repoint(cr, mapping)
    _drop_old_model(cr)
    _logger.info('Project OS upgrade: the project table is now ethara.project.')
