"""Retire the in-Odoo assessment engine for a link-out (data model v2 §4.6.2, §4.6.3).

v2 drops the quiz engine: no questions, no attempts, no scoring inside Odoo. The paper is
sat in an external application and a PM records the verdict by hand.

That means two models go — ``epo.assessment.link`` and ``epo.assessment.result`` — and
one arrives, ``ethara.assessment``. Dropping them naively would destroy every score in
the database, and the scores are not decoration: ``project.min_assessment_score`` is
enforced when somebody is allocated, and ``candidates()`` ranks the staffing shortlist by
them. So the order here is *preserve, then drop*:

1. Create an ``ethara.assessment`` row per live link, carrying the title and pass mark
   forward. The URL is unknown — the old link pointed at an API, not at a page a human
   opens — so it lands as a placeholder the GPM must correct, and every one is logged.
2. Copy each person's **best graded score** onto their ``epo.onboarding`` row, along with
   who graded it and when, so the staffing bar keeps working for everybody already
   assessed. This is the whole reason this migration is not three DROP statements.
3. Only then drop the old tables.

Idempotent: re-running finds no ``epo_assessment_link`` table and returns.
"""

import logging

_logger = logging.getLogger(__name__)

# Placeholder for the one fact the old schema could not give us: the human-facing page.
PLACEHOLDER_URL = 'https://example.invalid/assessment-url-needs-setting'

STALE_XMLIDS = {
    'ir.ui.view': ('view_epo_assessment_link_list', 'view_epo_assessment_link_form',
                   'view_epo_assessment_link_search', 'view_epo_assessment_result_list',
                   'view_epo_assessment_result_search'),
    'ir.actions.act_window': ('action_epo_assessment_link',
                              'action_epo_assessment_result'),
    'ir.ui.menu': ('menu_epo_assessment_results',),
    'ir.cron': ('cron_grade_pending', 'cron_assessment_sync',
                'cron_assessment_pull_results'),
    'ir.rule': ('rule_result_own', 'rule_result_pod', 'rule_result_manager'),
    'ir.model.access': ('access_epo_assessment_link_member',
                        'access_epo_assessment_link_manager',
                        'access_epo_assessment_link_admin',
                        'access_epo_assessment_result_member',
                        'access_epo_assessment_result_lead',
                        'access_epo_assessment_result_manager',
                        'access_epo_assessment_result_admin',
                        'access_etp_assessment_epo_manager',
                        'access_etp_assessment_evaluator_epo_manager'),
}

NEW_ONBOARDING_COLUMNS = [
    ('assessment_passed', 'boolean DEFAULT false'),
    ('assessment_verified_by', 'integer'),
    ('assessment_verified_at', 'timestamp'),
    ('assessment_score', 'double precision'),
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


def _create_new_model(cr):
    """The ORM builds ethara_assessment a moment later; we need it now to copy into."""
    cr.execute("""
        CREATE TABLE IF NOT EXISTS ethara_assessment (
            id           serial PRIMARY KEY,
            project_id   integer NOT NULL REFERENCES project_project(id) ON DELETE CASCADE,
            title        varchar NOT NULL,
            url          varchar NOT NULL,
            provider     varchar,
            pass_score   integer DEFAULT 60,
            notes        text,
            is_mandatory boolean DEFAULT true,
            sequence     integer DEFAULT 10,
            active       boolean DEFAULT true,
            create_uid   integer,
            write_uid    integer,
            create_date  timestamp,
            write_date   timestamp
        )
    """)


def _carry_links_forward(cr):
    """One ethara.assessment per live link. The URL cannot be recovered — say so."""
    cr.execute("""
        SELECT id, project_id, title_snapshot, pass_score, is_mandatory, source_system
          FROM epo_assessment_link
         WHERE active
      ORDER BY id
    """)
    rows = cr.fetchall()
    for _lid, project_id, title, pass_score, mandatory, source in rows:
        cr.execute("""
            INSERT INTO ethara_assessment (project_id, title, url, provider, pass_score,
                                           is_mandatory, notes, sequence, active,
                                           create_uid, write_uid, create_date, write_date)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, 10, true, 1, 1, now(), now())
        """, (project_id, title or 'External Assessment', PLACEHOLDER_URL,
              source or '', int(pass_score or 60), bool(mandatory),
              'Migrated from the retired in-Odoo assessment engine. The URL is a '
              'placeholder — set the real link to the external application.'))
        _logger.error(
            'Project OS upgrade: assessment "%s" on project %s was migrated to a '
            'link-out, but the old schema held no human-facing URL. It is set to a '
            'placeholder and MUST be corrected before anyone is sent to it.',
            title, project_id)
    _logger.info('Project OS upgrade: carried %s assessment link(s) forward.', len(rows))


def _preserve_scores(cr):
    """Each person's best graded score onto their onboarding row.

    Without this every score in the database is lost and the staffing bar
    (``min_assessment_score``, checked on allocation) has no input at all — it would
    silently stop excluding anybody.
    """
    for column, ddl in NEW_ONBOARDING_COLUMNS:
        if not _column_exists(cr, 'epo_onboarding', column):
            cr.execute(f'ALTER TABLE epo_onboarding ADD COLUMN "{column}" {ddl}')

    # Best graded attempt per (employee, project). DISTINCT ON needs the ordering to
    # match, so score desc picks the best and graded_at breaks ties deterministically.
    cr.execute("""
        WITH best AS (
            SELECT DISTINCT ON (r.employee_id, r.project_id)
                   r.employee_id, r.project_id, r.score, r.passed, r.graded_at
              FROM epo_assessment_result r
             WHERE r.state = 'graded'
          ORDER BY r.employee_id, r.project_id, r.score DESC, r.graded_at DESC
        )
        UPDATE epo_onboarding o
           SET assessment_passed      = best.passed,
               assessment_score       = best.score,
               assessment_verified_at = COALESCE(best.graded_at, now()),
               assessment_verified_by = 1
          FROM best
         WHERE o.employee_id = best.employee_id
           AND o.project_id  = best.project_id
    """)
    _logger.info('Project OS upgrade: preserved %s graded score(s) onto onboarding. '
                 'verified_by is set to the superuser — these were machine-graded '
                 'before the engine was retired, so no human asserted them.', cr.rowcount)

    # A passed verdict must carry a verifier (the model constrains it). Anything that
    # slipped through with a true flag and no stamp is cleared rather than left invalid.
    cr.execute("""
        UPDATE epo_onboarding SET assessment_passed = false
         WHERE assessment_passed AND assessment_verified_by IS NULL
    """)
    if cr.rowcount:
        _logger.warning('Project OS upgrade: cleared %s unstamped verdict(s).', cr.rowcount)


def _drop_stale_records(cr):
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

    # The remote-API settings have nothing left to configure.
    cr.execute("""
        DELETE FROM ir_config_parameter
         WHERE key IN ('epo.assessment.base_url', 'epo.assessment.token',
                       'epo.assessment.system')
    """)


def _drop_old_models(cr):
    cr.execute('DROP TABLE IF EXISTS epo_assessment_result CASCADE')
    cr.execute('DROP TABLE IF EXISTS epo_assessment_link CASCADE')
    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'ethara_project_os'
           AND (  (model = 'ir.model' AND name IN ('model_epo_assessment_link',
                                                   'model_epo_assessment_result'))
               OR (model = 'ir.model.fields'
                   AND (name LIKE 'field_epo_assessment_link__%%'
                     OR name LIKE 'field_epo_assessment_result__%%')))
    """)
    cr.execute("DELETE FROM ir_model WHERE model IN ('epo.assessment.link', "
               "'epo.assessment.result')")
    _logger.info('Project OS upgrade: the in-Odoo assessment engine is gone.')


def migrate(cr, version):
    if not version:
        return
    if not _table_exists(cr, 'epo_assessment_link'):
        _logger.info('Project OS upgrade: assessment engine already retired.')
        _drop_stale_records(cr)
        return

    _create_new_model(cr)
    _carry_links_forward(cr)
    _preserve_scores(cr)
    _drop_stale_records(cr)
    _drop_old_models(cr)
