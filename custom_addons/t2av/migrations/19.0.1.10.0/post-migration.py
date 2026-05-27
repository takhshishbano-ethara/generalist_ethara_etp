import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info(
        "T2AV 19.0.1.10.0: added Bedrock API Key (ABSK...) auth path. "
        "No schema changes; credential_manager now includes "
        "t2av.bedrock_api_key (encrypted) as a single-key Bedrock auth "
        "alternative to AWS Access Key + Secret Key."
    )
