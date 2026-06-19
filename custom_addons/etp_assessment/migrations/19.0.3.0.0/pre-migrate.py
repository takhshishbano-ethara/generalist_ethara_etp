# Migration for the hr.employee -> hr.applicant candidate-identity change.
#
# Two prod-only hazards this heals automatically on `-u etp_assessment`:
#   1. Existing transactional rows (evaluator/response/day-session) hold old
#      hr.employee ids in evaluator_id, which now FK to hr.applicant -> the
#      upgrade would roll back with a FK violation. We clear those attempt
#      rows (definition data: assessments/skills/questions/categories is KEPT).
#   2. mail.template + ir.rule records are loaded noupdate="1", so `-u` will
#      NOT refresh their bodies/domains -> they keep stale `employee_id`
#      references (broken invites + broken candidate isolation). We delete the
#      affected records + their ir_model_data here (pre-load) so the clean,
#      applicant-based XML RECREATES them later in this same upgrade.
import logging

_logger = logging.getLogger(__name__)


def _table_exists(cr, table):
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return

    for table in (
        "etp_assessment_response_line",
        "etp_assessment_response",
        "etp_assessment_day_session",
        "etp_assessment_evaluator",
    ):
        if _table_exists(cr, table):
            cr.execute('DELETE FROM "%s"' % table)
            _logger.info(
                "etp_assessment 19.0.3.0.0: cleared %s (%s rows)",
                table, cr.rowcount,
            )

    # Old evaluator_ids M2M (etp.assessment <-> hr.employee) link rows.
    for rel in ("etp_assessment_hr_employee_rel", "etp_assessment_evaluator_ids_rel"):
        if _table_exists(cr, rel):
            cr.execute('DELETE FROM "%s"' % rel)
            _logger.info(
                "etp_assessment 19.0.3.0.0: cleared M2M %s (%s rows)",
                rel, cr.rowcount,
            )

    tmpl_names = (
        "mail_template_day_invitation",
        "mail_template_single_invitation",
    )
    cr.execute(
        """
        DELETE FROM mail_template WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'etp_assessment' AND model = 'mail.template'
              AND name IN %s)
        """,
        (tmpl_names,),
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
        WHERE module = 'etp_assessment' AND model = 'mail.template'
          AND name IN %s
        """,
        (tmpl_names,),
    )

    rule_names = (
        "rule_assessment_evaluator_own_records_evaluator",
        "rule_assessment_day_session_own_evaluator",
        "rule_assessment_response_own_evaluator",
    )
    cr.execute(
        """
        DELETE FROM ir_rule WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'etp_assessment' AND model = 'ir.rule'
              AND name IN %s)
        """,
        (rule_names,),
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
        WHERE module = 'etp_assessment' AND model = 'ir.rule'
          AND name IN %s
        """,
        (rule_names,),
    )
    _logger.info(
        "etp_assessment 19.0.3.0.0: stale invitation templates + isolation "
        "rules cleared; clean applicant-based XML will recreate them."
    )
