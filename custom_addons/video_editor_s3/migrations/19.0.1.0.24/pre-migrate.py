import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE ir_config_parameter
           SET value = 'video_editor_s3'
         WHERE key = 'video_editor_s3.youtube_prefix'
           AND value = 'video_editor_s3/youtube'
    """)
    _logger.info(
        "[video_editor_s3] 19.0.1.0.24: flattened youtube_prefix ICP "
        "from 'video_editor_s3/youtube' to 'video_editor_s3' (%d row(s) updated)",
        cr.rowcount,
    )
