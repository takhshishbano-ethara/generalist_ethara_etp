def migrate(cr, version):
    cr.execute(
        """
        DELETE FROM ir_cron
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'lynceus'
              AND name = 'cron_lynceus_pool_depletion_alert'
        )
        """
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
        WHERE module = 'lynceus'
          AND name = 'cron_lynceus_pool_depletion_alert'
        """
    )
