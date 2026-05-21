"""Migration 19.0.1.x -> 19.0.2.0.0.

- Rename column ``via_rabbitmq`` -> ``via_batch`` on ``gohan_job``.
  Done in pre-migration so the new ORM field definition matches the
  existing column at update time.
- Migrate config params: ``gohan.extraction_service_url`` (Lambda
  Function URL) is no longer used. We do NOT delete it — admins can
  copy the Lambda function name out of it after upgrade if needed.
- Insert default values for the new config params so the first batch
  run after upgrade doesn't crash with a missing setting.
"""


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'gohan_job'
           AND column_name = 'via_rabbitmq'
        """
    )
    has_old = bool(cr.fetchone())

    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'gohan_job'
           AND column_name = 'via_batch'
        """
    )
    has_new = bool(cr.fetchone())

    if has_old and not has_new:
        cr.execute(
            "ALTER TABLE gohan_job RENAME COLUMN via_rabbitmq TO via_batch"
        )
    elif has_old and has_new:
        cr.execute(
            """
            UPDATE gohan_job
               SET via_batch = via_rabbitmq
             WHERE via_rabbitmq = TRUE AND via_batch = FALSE
            """
        )
        cr.execute("ALTER TABLE gohan_job DROP COLUMN via_rabbitmq")

    cr.execute(
        """
        INSERT INTO ir_config_parameter (key, value)
        SELECT 'gohan.batch_concurrency', '250'
        WHERE NOT EXISTS (
            SELECT 1 FROM ir_config_parameter
             WHERE key = 'gohan.batch_concurrency'
        )
        """
    )
    cr.execute(
        """
        INSERT INTO ir_config_parameter (key, value)
        SELECT 'gohan.lambda_region', 'ap-south-1'
        WHERE NOT EXISTS (
            SELECT 1 FROM ir_config_parameter
             WHERE key = 'gohan.lambda_region'
        )
        """
    )
    cr.execute(
        """
        INSERT INTO ir_config_parameter (key, value)
        SELECT 'gohan.watchdog_extracting_minutes', '60'
        WHERE NOT EXISTS (
            SELECT 1 FROM ir_config_parameter
             WHERE key = 'gohan.watchdog_extracting_minutes'
        )
        """
    )
    cr.execute(
        """
        INSERT INTO ir_config_parameter (key, value)
        SELECT 'gohan.watchdog_generating_minutes', '45'
        WHERE NOT EXISTS (
            SELECT 1 FROM ir_config_parameter
             WHERE key = 'gohan.watchdog_generating_minutes'
        )
        """
    )

    cr.execute(
        """
        DELETE FROM ir_act_server
         WHERE id IN (
            SELECT res_id FROM ir_model_data
             WHERE module = 'gohan'
               AND name   = 'action_gohan_run_via_rabbitmq'
               AND model  = 'ir.actions.server'
         )
        """
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'gohan'
           AND name   = 'action_gohan_run_via_rabbitmq'
        """
    )
