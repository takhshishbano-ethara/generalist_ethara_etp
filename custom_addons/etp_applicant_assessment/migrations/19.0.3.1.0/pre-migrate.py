import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    if not _table_exists(cr, "etp_applicant_assessment_question_bank"):
        return

    _drop_column(cr, "etp_applicant_assessment_question_bank", "sequence")
    _drop_column(cr, "etp_applicant_assessment_question_bank", "option_count")

    _add_char_column(cr, "etp_applicant_assessment_question_bank", "tags")
    _add_char_column(cr, "etp_applicant_assessment_question_bank", "skills")

    _backfill_tags_from_legacy_m2m(cr)

    _drop_legacy_m2m_tables(cr)
    _drop_table(cr, "etp_applicant_assessment_question_bank_tag")
    _drop_table(cr, "etp_applicant_assessment_question_bank_skill")

    cr.execute(
        """
        DELETE FROM ir_model_data
        WHERE module = 'etp_applicant_assessment'
          AND name IN (
            'menu_applicant_assessment_bank_tags',
            'menu_applicant_assessment_bank_skills',
            'action_question_bank_tag',
            'action_question_bank_skill',
            'view_question_bank_tag_list',
            'view_question_bank_skill_list',
            'access_question_bank_tag_user',
            'access_question_bank_tag_manager',
            'access_question_bank_skill_user',
            'access_question_bank_skill_manager'
          )
        """
    )
    cr.execute(
        """
        DELETE FROM ir_model
        WHERE model IN (
            'etp.applicant.assessment.question.bank.tag',
            'etp.applicant.assessment.question.bank.skill'
        )
        """
    )


def _table_exists(cr, table):
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return bool(cr.fetchone())


def _drop_column(cr, table, column):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    if cr.fetchone():
        cr.execute('ALTER TABLE "{}" DROP COLUMN "{}"'.format(table, column))
        _logger.info("Dropped column %s.%s", table, column)


def _add_char_column(cr, table, column):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    if not cr.fetchone():
        cr.execute('ALTER TABLE "{}" ADD COLUMN "{}" VARCHAR'.format(table, column))
        _logger.info("Added column %s.%s", table, column)


def _drop_table(cr, table):
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    if cr.fetchone():
        cr.execute('DROP TABLE "{}" CASCADE'.format(table))
        _logger.info("Dropped table %s", table)


def _find_legacy_tag_rel_table(cr):
    cr.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name LIKE 'etp_applicant_assessment_question_bank%%tag%%rel'
        ORDER BY length(table_name) DESC
        LIMIT 1
        """
    )
    row = cr.fetchone()
    return row[0] if row else None


def _backfill_tags_from_legacy_m2m(cr):
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        ("etp_applicant_assessment_question_bank_tag",),
    )
    if not cr.fetchone():
        return
    rel_table = _find_legacy_tag_rel_table(cr)
    if not rel_table:
        return

    cr.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (rel_table,),
    )
    cols = [r[0] for r in cr.fetchall()]
    tag_col = next((c for c in cols if "tag_id" in c), None)
    bank_col = next((c for c in cols if c != tag_col and c.endswith("_id")), None)
    if not tag_col or not bank_col:
        return

    cr.execute(
        """
        SELECT rel.{bank}, string_agg(tag.name, ', ' ORDER BY tag.name)
        FROM {rel} rel
        JOIN etp_applicant_assessment_question_bank_tag tag ON tag.id = rel.{tag}
        JOIN etp_applicant_assessment_question_bank bank ON bank.id = rel.{bank}
        WHERE bank.tags IS NULL OR bank.tags = ''
        GROUP BY rel.{bank}
        """.format(rel=rel_table, bank=bank_col, tag=tag_col)
    )
    updates = cr.fetchall()
    for bank_id, tags_str in updates:
        cr.execute(
            "UPDATE etp_applicant_assessment_question_bank SET tags = %s WHERE id = %s",
            (tags_str, bank_id),
        )
    if updates:
        _logger.info("Backfilled tags char for %d bank rows from legacy m2m", len(updates))


def _drop_legacy_m2m_tables(cr):
    cr.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
          AND (table_name LIKE 'etp_applicant_assessment_question_bank%%tag%%rel'
               OR table_name LIKE 'etp_applicant_assessment_question_bank%%skill%%rel')
        """
    )
    for (tbl,) in cr.fetchall():
        cr.execute('DROP TABLE "{}" CASCADE'.format(tbl))
        _logger.info("Dropped legacy m2m rel table %s", tbl)
