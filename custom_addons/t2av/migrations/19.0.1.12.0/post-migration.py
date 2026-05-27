import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info(
        "T2AV 19.0.1.12.0 post-migration: validator importlib fix + ARN masking. "
        "No schema changes (model_id_display is a computed non-stored field)."
    )
