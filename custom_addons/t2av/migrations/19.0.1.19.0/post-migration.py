"""19.0.1.19.0: deactivate reap-locks cron (its XML is noupdate=1 so `-u t2av`
does not touch it) and purge group_t2av_reviewer memberships before the group
record is dropped."""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        DELETE FROM res_groups_users_rel
         WHERE gid IN (
             SELECT res_id FROM ir_model_data
              WHERE module = 't2av'
                AND model = 'res.groups'
                AND name = 'group_t2av_reviewer'
         )
        """
    )
    removed = cr.rowcount
    _logger.info(
        "T2AV 19.0.1.19.0: removed %s user(s) from group_t2av_reviewer",
        removed,
    )

    cr.execute(
        """
        UPDATE ir_cron
           SET active = False
         WHERE id IN (
             SELECT res_id FROM ir_model_data
              WHERE module = 't2av'
                AND model = 'ir.cron'
                AND name = 'ir_cron_t2av_reap_stale_stack_locks'
         )
        """
    )
    _logger.info(
        "T2AV 19.0.1.19.0: deactivated ir_cron_t2av_reap_stale_stack_locks (rowcount=%s)",
        cr.rowcount,
    )

    for rule_xmlid in (
        "rule_t2av_generation_reviewer",
        "rule_t2av_attempt_reviewer",
        "rule_t2av_video_review_reviewer",
    ):
        cr.execute(
            """
            DELETE FROM rule_group_rel
             WHERE rule_group_id IN (
                 SELECT res_id FROM ir_model_data
                  WHERE module='t2av' AND model='ir.rule' AND name=%s
             )
            """,
            (rule_xmlid,),
        )
        cr.execute(
            """
            DELETE FROM ir_rule
             WHERE id IN (SELECT res_id FROM ir_model_data
                           WHERE module='t2av' AND model='ir.rule' AND name=%s)
            """,
            (rule_xmlid,),
        )
        cr.execute(
            """
            DELETE FROM ir_model_data
             WHERE module='t2av' AND model='ir.rule' AND name=%s
            """,
            (rule_xmlid,),
        )

    cr.execute(
        """
        DELETE FROM res_groups_implied_rel
         WHERE gid IN (SELECT res_id FROM ir_model_data
                        WHERE module='t2av' AND model='res.groups'
                          AND name='group_t2av_reviewer')
            OR hid IN (SELECT res_id FROM ir_model_data
                        WHERE module='t2av' AND model='res.groups'
                          AND name='group_t2av_reviewer')
        """
    )
    cr.execute(
        """
        DELETE FROM res_groups
         WHERE id IN (SELECT res_id FROM ir_model_data
                       WHERE module='t2av' AND model='res.groups'
                         AND name='group_t2av_reviewer')
        """
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module='t2av' AND model='res.groups'
           AND name='group_t2av_reviewer'
        """
    )
    _logger.info(
        "T2AV 19.0.1.19.0: dropped group_t2av_reviewer (noupdate=1 prevented "
        "auto-cleanup; rowcount=%s)",
        cr.rowcount,
    )
