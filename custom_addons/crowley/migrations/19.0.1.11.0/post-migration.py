import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info(
        "Crowley 19.0.1.11.0: dropped topP from Bedrock inferenceConfig in "
        "enrichment + review clients. Sonnet 4.6 application-inference-profile "
        "rejects both temperature and top_p set simultaneously. "
        "Only temperature is now sent to Bedrock; top_p is still forwarded to "
        "OpenRouter (Kimi K2) for video review. No schema changes."
    )
