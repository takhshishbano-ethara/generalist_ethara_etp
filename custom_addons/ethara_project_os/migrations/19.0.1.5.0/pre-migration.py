"""Rename the role vocabulary to match the organisation's — and ``pod_roles``.

    gpm  ->  pm        (PM, Programme Manager — the level above Pod Lead)
    pm   ->  tasker    (Tasker — the bottom level, previously "pod member")

The old names were not merely untidy, they were invertible. ``pm`` meant the *weakest*
level in this module and the *second-strongest* in ``pod_roles``, in ``api.role`` and in
conversation. Every place a role string crossed that boundary — an API response body, a
``min_role`` check, a stored selection — could be read with exactly inverted privilege,
and it would fail silently rather than loudly, because ``pm`` is a value both sides
recognise.

``pm`` is therefore both a *source* and a *target* of this rename. Every step below is
written so that cannot collapse:

* the stored selection values move in ONE ``CASE`` expression per column, evaluated
  per row, so there is no intermediate state in which a manager has already become a
  ``pm`` and is then read again as one and demoted to ``tasker``;
* the whole script is gated on a sentinel that this migration itself removes, so a
  second run cannot re-apply the swap and turn every manager into a Tasker.

Three things must happen before the ORM loads the new data files:

1. **The group xml-ids.** ``group_epo_member`` → ``group_epo_tasker`` and
   ``group_epo_manager`` → ``group_epo_pm``. Without this the loader finds no record for
   the new xml-ids and CREATES two empty groups, leaving the originals orphaned with
   every membership still attached to them — so all 16 users silently lose their access
   level while the upgrade reports success. This is the step that makes the rest safe.

2. **The ``gpm_id`` column** on ``project_project`` → ``pm_id``, together with its
   ``ir.model.fields`` row and xml-id. Otherwise the ORM adds an empty ``pm_id`` beside
   the populated ``gpm_id`` and every project silently loses its owner.

3. **The stored selection values** in the three columns that hold a level:
   ``hr_employee.epo_role`` (computed+stored, so it would be recomputed anyway, but it
   must not be invalid in the meantime), ``epo_role_assignment.role`` (the grant — the
   real source of truth) and ``epo_allocation.role_on_project``.
"""

import logging

_logger = logging.getLogger(__name__)

# old xml-id -> new xml-id, for res.groups records owned by this module.
GROUP_RENAMES = [
    ('group_epo_member', 'group_epo_tasker'),
    ('group_epo_manager', 'group_epo_pm'),
]

# (table, column) holding a Project OS level as a selection value.
LEVEL_COLUMNS = [
    ('hr_employee', 'epo_role'),
    ('epo_role_assignment', 'role'),
    ('epo_allocation', 'role_on_project'),
]


def _table_exists(cr, table):
    cr.execute('SELECT to_regclass(%s)', (f'public.{table}',))
    return cr.fetchone()[0] is not None


def _column_exists(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def _rename_groups(cr):
    """Point the existing group records at their new xml-ids.

    Renaming ``ir_model_data`` rather than the groups themselves is the whole trick:
    membership lives in ``res_groups_users_rel`` keyed on the group id, which never
    changes, so nobody is touched. The loader then UPDATEs the record it already knows
    about instead of INSERTing a replacement.
    """
    for old, new in GROUP_RENAMES:
        cr.execute("""
            SELECT 1 FROM ir_model_data
             WHERE module = 'ethara_project_os' AND model = 'res.groups' AND name = %s
        """, (new,))
        if cr.fetchone():
            # A previous partial run already created it; renaming onto it would break
            # the (module, name) unique index.
            _logger.warning('Project OS upgrade: %s already exists, leaving %s alone.',
                            new, old)
            continue
        cr.execute("""
            UPDATE ir_model_data SET name = %s
             WHERE module = 'ethara_project_os' AND model = 'res.groups' AND name = %s
        """, (new, old))
        if cr.rowcount:
            _logger.info('Project OS upgrade: group %s -> %s (%s membership(s) kept).',
                         old, new, _group_member_count(cr, new))


def _group_member_count(cr, xml_id):
    cr.execute("""
        SELECT count(*) FROM res_groups_users_rel r
          JOIN ir_model_data d ON d.res_id = r.gid
         WHERE d.module = 'ethara_project_os' AND d.model = 'res.groups' AND d.name = %s
    """, (xml_id,))
    return cr.fetchone()[0]


def _rename_owner_field(cr):
    """project_project.gpm_id -> pm_id, column + ir.model.fields + xml-id."""
    if not _column_exists(cr, 'project_project', 'gpm_id'):
        _logger.info('Project OS upgrade: gpm_id already renamed.')
        return
    if _column_exists(cr, 'project_project', 'pm_id'):
        _logger.error(
            'Project OS upgrade: both gpm_id and pm_id exist on project_project. '
            'Refusing to guess which holds the owner — resolve by hand.')
        raise RuntimeError('project_project has both gpm_id and pm_id')

    cr.execute('ALTER TABLE project_project RENAME COLUMN gpm_id TO pm_id')
    cr.execute("""
        UPDATE ir_model_fields SET name = 'pm_id'
         WHERE model = 'project.project' AND name = 'gpm_id'
    """)
    cr.execute("""
        UPDATE ir_model_data SET name = 'field_project_project__pm_id'
         WHERE module = 'ethara_project_os'
           AND model = 'ir.model.fields'
           AND name = 'field_project_project__gpm_id'
    """)
    cr.execute('SELECT count(*) FROM project_project WHERE pm_id IS NOT NULL')
    _logger.info('Project OS upgrade: gpm_id -> pm_id, %s project owner(s) preserved.',
                 cr.fetchone()[0])


def _fix_mail_templates(cr):
    """Rewrite ``gpm_id`` inside the stored mail template bodies.

    ``data/epo_mail_templates.xml`` is ``noupdate="1"`` — deliberately, so an operator's
    edits to the wording survive an upgrade. The consequence is that editing the XML does
    **not** refresh the row already in the database, so the allocation notice kept
    rendering ``object.project_id.gpm_id`` against a field that no longer exists. Every
    allocation then logged a QWebError and silently sent no email.

    ``body_html`` is jsonb (``translate=True``), so the replacement is done on the whole
    document and cast back — that keeps every language key intact, which a per-key update
    would not. ``gpm_id`` cannot occur in jsonb's own syntax, so a text-level replace is
    safe here.

    Scoped to this module's own templates: another module's template that happened to
    contain the same substring is none of our business.
    """
    cr.execute("SELECT to_regclass('public.mail_template')")
    if not cr.fetchone()[0]:
        return
    for column in ('body_html', 'subject'):
        cr.execute("""
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'mail_template' AND column_name = %s
        """, (column,))
        if not cr.fetchone():
            continue
        cr.execute(f"""
            UPDATE mail_template t
               SET "{column}" = replace(t."{column}"::text, 'gpm_id', 'pm_id')::jsonb
             WHERE t."{column}"::text LIKE '%%gpm_id%%'
               AND t.id IN (SELECT res_id FROM ir_model_data
                             WHERE module = 'ethara_project_os'
                               AND model = 'mail.template')
        """)
        if cr.rowcount:
            _logger.info('Project OS upgrade: mail_template.%s — %s row(s) repointed '
                         'from gpm_id to pm_id.', column, cr.rowcount)


def _swap_levels(cr):
    """Move the stored selection values, atomically per row.

    One CASE, not two UPDATEs. Two statements in either order would be wrong: 'gpm'
    first turns managers into 'pm' which the second statement then demotes to 'tasker';
    'pm' first is correct only until someone reorders the file. CASE removes the
    question — each row is evaluated once against its original value.
    """
    for table, column in LEVEL_COLUMNS:
        if not _table_exists(cr, table) or not _column_exists(cr, table, column):
            continue
        cr.execute(f"""
            UPDATE {table}
               SET "{column}" = CASE "{column}"
                                    WHEN 'pm'  THEN 'tasker'
                                    WHEN 'gpm' THEN 'pm'
                                    ELSE "{column}"
                                END
             WHERE "{column}" IN ('pm', 'gpm')
        """)
        _logger.info('Project OS upgrade: %s.%s — %s row(s) remapped.',
                     table, column, cr.rowcount)


def migrate(cr, version):
    if not version:
        return

    # The sentinel. `group_epo_member` is renamed away below and never comes back, so
    # its presence means "not yet migrated" and its absence means "done". Gating the
    # whole script on it is what makes the level swap safe to have in a file that a
    # future operator might be tempted to re-run: re-applying the CASE would read every
    # freshly-renamed manager as a 'pm' and demote them to 'tasker'.
    cr.execute("""
        SELECT 1 FROM ir_model_data
         WHERE module = 'ethara_project_os' AND model = 'res.groups'
           AND name = 'group_epo_member'
    """)
    if not cr.fetchone():
        _logger.info('Project OS upgrade: role vocabulary already renamed, nothing to do.')
        return

    _logger.info('Project OS upgrade: renaming the role vocabulary '
                 '(gpm -> pm, pm -> tasker).')
    _swap_levels(cr)          # before the sentinel disappears
    _rename_owner_field(cr)
    _fix_mail_templates(cr)   # must follow the field rename it repoints to
    _rename_groups(cr)        # removes the sentinel, so this goes last
