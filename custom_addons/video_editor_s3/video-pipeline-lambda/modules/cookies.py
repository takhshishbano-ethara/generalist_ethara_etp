import logging
import os

import config

_logger = logging.getLogger(__name__)
_COOKIES_PATH = "/tmp/youtube_cookies.txt"
_cookies_loaded = {"done": False}


def cookies_file_path():
    if _cookies_loaded["done"] and os.path.exists(_COOKIES_PATH):
        return _COOKIES_PATH
    arn = config.YOUTUBE_COOKIES_SECRET_ARN
    if not arn:
        _logger.info("YOUTUBE_COOKIES_SECRET_ARN unset; running without cookies")
        return None
    import boto3
    client = boto3.client("secretsmanager", region_name=config.S3_REGION)
    resp = client.get_secret_value(SecretId=arn)
    body = resp.get("SecretString")
    if not body:
        _logger.warning("youtube-cookies secret has empty SecretString")
        return None
    with open(_COOKIES_PATH, "w") as fh:
        fh.write(body)
    os.chmod(_COOKIES_PATH, 0o600)
    _cookies_loaded["done"] = True
    _logger.info("youtube cookies materialised at %s (%d bytes)", _COOKIES_PATH, len(body))
    return _COOKIES_PATH
