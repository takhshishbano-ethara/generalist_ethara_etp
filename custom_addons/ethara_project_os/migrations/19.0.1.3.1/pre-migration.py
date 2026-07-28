"""Close the NULL hole in the go-live gate — and make it safe to close.

The gate is a table constraint:

    CHECK (ethara_state <> 'active' OR (has_sop AND has_stagelist))

In SQL ``NULL AND NULL`` is ``NULL``, and **a CHECK passes on NULL**. Both columns are
nullable, so a project could be stored ``active`` with no SOP and no published stagelist
— defeating the one invariant the module exists to enforce. Verified by hand: the UPDATE
succeeds today.

The constraint is tightened to ``COALESCE(..., false)`` in the model. That alone would be
dangerous, because of how Odoo reacts when a constraint cannot be applied
(``registry.py:706`` / ``finalize_constraints``):

* on install  → ``_schema.error``  — logged, not raised
* on upgrade  → ``_schema.info`` then a retry, and on failure ``_schema.warning``
  with the comment *"warn only, this is not a deployment showstopper"*

So a single offending row would leave the upgrade looking successful with **no constraint
at all** — and the old, weaker one already dropped. Worse than the bug.

Hence this script, which runs before the ORM touches constraints:

0. Drop the old constraint. It would otherwise reject the cleanup itself — backfilling
   a NULL flag to false on an active row turns a state the old CHECK tolerated into one
   it forbids. The ORM re-adds the tightened version afterwards.
1. Backfill NULL gate flags to false.
2. **Recompute both flags from actual content.** A flag that disagrees with the documents
   and templates in the database is the real defect; the NULL was only how it surfaced.
3. Demote any project still ``active`` without both flags, loudly. Such a project is
   already invalid by the module's own rules, and letting one row hold the constraint
   hostage is how the gate silently disappears.

Idempotent: a second run finds nothing to change.
"""

import logging

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return
    for column in ('has_sop', 'has_stagelist', 'is_project_os', 'ethara_state'):
        if not _column_exists(cr, 'project_project', column):
            _logger.info('Project OS upgrade: %s not present yet, nothing to backfill.',
                         column)
            return

    # 0. Drop the OLD constraint before touching the data.
    #
    #    Found by running this migration against a planted bad row: the old CHECK is
    #    still on the table while these updates run, and backfilling has_sop from NULL
    #    to false on an `active` row turns a row the old CHECK tolerated (NULL passes)
    #    into one it rejects (false fails). The cleanup cannot proceed underneath it.
    #
    #    Dropping is safe: the ORM re-adds the constraint later in the same upgrade,
    #    with the new definition, and step 4 refuses to continue unless the table is
    #    clean enough for that to succeed.
    cr.execute('ALTER TABLE project_project '
               'DROP CONSTRAINT IF EXISTS project_project_epo_active_requires_gate')

    # 1. NULL is the hole. Close it everywhere, not just on Ethara rows: a non-Ethara
    #    project with NULL flags is what a future migration would trip over.
    cr.execute("""
        UPDATE project_project
           SET has_sop = COALESCE(has_sop, false),
               has_stagelist = COALESCE(has_stagelist, false)
         WHERE has_sop IS NULL OR has_stagelist IS NULL
    """)
    if cr.rowcount:
        _logger.info('Project OS upgrade: backfilled NULL gate flags on %s row(s).',
                     cr.rowcount)

    # 2. Recompute from the content that is actually there. Mirrors
    #    project_project._recompute_gate() — an SOP document in the sop folder, and a
    #    published stagelist template.
    cr.execute("""
        UPDATE project_project p
           SET has_sop = EXISTS (SELECT 1 FROM epo_document d
                                  WHERE d.project_id = p.id
                                    AND d.category = 'sop' AND d.active),
               has_stagelist = EXISTS (SELECT 1 FROM epo_form_template t
                                        WHERE t.project_id = p.id
                                          AND t.form_type = 'stagelist'
                                          AND t.state = 'published')
         WHERE p.is_project_os
    """)
    _logger.info('Project OS upgrade: recomputed the gate flags on %s project(s).',
                 cr.rowcount)

    # 3. Anything still claiming to be live without both flags cannot legally exist. It
    #    is demoted rather than left to block the constraint — and named, because a
    #    project dropping out of `active` is something an operator must know about.
    cr.execute("""
        SELECT id, name ->> 'en_US' FROM project_project
         WHERE ethara_state = 'active' AND NOT (has_sop AND has_stagelist)
    """)
    offenders = cr.fetchall()
    for project_id, name in offenders:
        _logger.error(
            'Project OS upgrade: project %s ("%s") was marked active but has no SOP '
            'and/or no published stagelist. It has been returned to setup — it could '
            'not have passed the go-live gate. Add the missing content and activate it '
            'again.', project_id, name)
    if offenders:
        cr.execute("""
            UPDATE project_project SET ethara_state = 'setup'
             WHERE ethara_state = 'active' AND NOT (has_sop AND has_stagelist)
        """)

    # 4. Prove the table is now clean, so the ORM's silent-failure path is never reached.
    cr.execute("""
        SELECT count(*) FROM project_project
         WHERE ethara_state <> 'setup'
           AND NOT (COALESCE(has_sop, false) AND COALESCE(has_stagelist, false))
           AND ethara_state = 'active'
    """)
    remaining = cr.fetchone()[0]
    if remaining:
        raise RuntimeError(
            'Project OS upgrade: %s project(s) still violate the go-live gate after '
            'cleanup. Refusing to continue rather than let the constraint be dropped '
            'and silently not re-applied.' % remaining)
    _logger.info('Project OS upgrade: the go-live gate can be tightened safely.')
