import json
import logging

_logger = logging.getLogger(__name__)
_clients = {}


def _get_client(region, access_key=None, secret_key=None):
    cache_key = (region, access_key or "")
    if cache_key in _clients:
        return _clients[cache_key]
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required for Lambda invocation: %s" % exc) from exc
    kwargs = {"region_name": region}
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    client = boto3.client("lambda", **kwargs)
    _clients[cache_key] = client
    return client


def invoke_async(function_name, region, payload, access_key=None, secret_key=None):
    client = _get_client(region, access_key, secret_key)
    body = json.dumps(payload).encode("utf-8")
    resp = client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=body,
    )
    api_request_id = (resp.get("ResponseMetadata") or {}).get("RequestId") or ""
    status = resp.get("StatusCode")
    if status not in (200, 202):
        raise RuntimeError("lambda invoke returned status %s for %s" % (status, function_name))
    _logger.info(
        "lambda invoke async function=%s region=%s api_request_id=%s",
        function_name, region, api_request_id,
    )
    return api_request_id
