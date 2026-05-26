import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info(
        "T2AV 19.0.1.8.0: auto-retry-with-failure-reason feature added. "
        "No schema changes; behavior change only in enrichment worker."
    )
