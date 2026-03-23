import os
import logging

_logger = logging.getLogger(__name__)

_env_loaded = False


def _load_env():
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        from dotenv import load_dotenv

        _addons_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _custom_addons_dir = os.path.dirname(_addons_dir)
        env_path = os.path.join(_custom_addons_dir, "preference_ranking", ".env")
        if os.path.isfile(env_path):
            load_dotenv(env_path)
            _logger.info("ai_services: loaded .env from %s", env_path)
    except ImportError:
        pass


def get_bedrock_config():
    _load_env()
    api_key = os.environ.get("AI_SERVICES_BEDROCK_API_KEY", "").strip()
    inference_arn = os.environ.get("AI_SERVICES_BEDROCK_INFERENCE_ARN", "").strip()
    region = os.environ.get("AI_SERVICES_BEDROCK_REGION", "ap-south-1").strip()
    return {
        "api_key": api_key,
        "inference_arn": inference_arn,
        "region": region,
    }
