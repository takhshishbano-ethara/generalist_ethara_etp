import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        SELECT key, value FROM ir_config_parameter
         WHERE key IN ('t2av.bedrock_model_id', 't2av.bedrock_review_model_id')
    """)
    rows = {k: v for k, v in cr.fetchall()}
    enrichment_val = rows.get('t2av.bedrock_model_id') or ''
    review_val = rows.get('t2av.bedrock_review_model_id') or ''

    if not enrichment_val.strip() and review_val.strip():
        cr.execute(
            "UPDATE ir_config_parameter SET value = %s WHERE key = %s",
            (review_val, 't2av.bedrock_model_id'),
        )
        if cr.rowcount == 0:
            cr.execute(
                "INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s)",
                ('t2av.bedrock_model_id', review_val),
            )
        _logger.info(
            "T2AV 19.0.1.13.0: backfilled bedrock_model_id from old bedrock_review_model_id"
        )

    cr.execute(
        "DELETE FROM ir_config_parameter WHERE key = %s",
        ('t2av.bedrock_review_model_id',),
    )
    if cr.rowcount:
        _logger.info(
            "T2AV 19.0.1.13.0: removed legacy ICP t2av.bedrock_review_model_id"
        )
