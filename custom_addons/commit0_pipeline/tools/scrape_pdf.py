import base64
import bz2
import json
import logging
import os
import urllib.request
import urllib.error
from urllib.parse import urlparse

_log = logging.getLogger(__name__)


def _save_with_bz2(data: bytes, pdf_path: str) -> None:
    with open(pdf_path, "wb") as f:
        f.write(data)
    with open(pdf_path + ".bz2", "wb") as f:
        f.write(bz2.compress(data))


def _try_direct_pdf_download(spec_url, repo_name, output_dir):
    candidates = []
    parsed = urlparse(spec_url)
    host = parsed.hostname or ""

    if "readthedocs.io" in host or "readthedocs.org" in host:
        project = host.split(".")[0]
        candidates.append(
            f"https://{project}.readthedocs.io/_/downloads/en/latest/pdf/"
        )
        candidates.append(
            f"https://{project}.readthedocs.io/_/downloads/en/stable/pdf/"
        )

    if spec_url.lower().endswith(".pdf"):
        candidates.append(spec_url)

    pdf_path = os.path.join(output_dir, f"{repo_name}.pdf")

    for url in candidates:
        try:
            _log.info("Trying direct PDF download: %s", url)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=60)
            ctype = resp.headers.get("Content-Type", "")
            if "pdf" not in ctype.lower():
                continue
            data = resp.read()
            if len(data) < 500:
                continue
            _save_with_bz2(data, pdf_path)
            _log.info("Direct PDF download OK (%d bytes): %s", len(data), url)
            return pdf_path
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.debug("Direct download failed for %s: %s", url, exc)
    return None


def _call_lambda_pdf(
    function_name,
    spec_url,
    repo_name,
    full_name,
    github_token="",
    region="",
    access_key="",
    secret_key="",
):
    import boto3

    client = boto3.client(
        "lambda",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )

    payload = json.dumps(
        {
            "spec_url": spec_url or "",
            "repo_name": repo_name,
            "full_name": full_name or "",
            "github_token": github_token or "",
            "compress": True,
        }
    )

    response = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=payload,
    )

    result = json.loads(response["Payload"].read().decode("utf-8"))

    if isinstance(result.get("body"), str):
        result = json.loads(result["body"])

    if result.get("statusCode") and result["statusCode"] >= 400:
        error_body = result.get("body", "")
        if isinstance(error_body, str):
            try:
                error_body = json.loads(error_body).get("error", error_body)
            except (json.JSONDecodeError, AttributeError):
                pass
        raise RuntimeError(
            "Lambda returned %s: %s" % (result["statusCode"], error_body)
        )

    pdf_b64 = result.get("pdf_base64")
    if not pdf_b64:
        return None

    return base64.b64decode(pdf_b64)


def scrape_spec_sync(
    spec_url,
    repo_name,
    output_dir,
    github_token="",
    full_name="",
    lambda_function_name="",
    lambda_region="",
    lambda_access_key="",
    lambda_secret_key="",
):
    os.makedirs(output_dir, exist_ok=True)

    if spec_url:
        path = _try_direct_pdf_download(spec_url, repo_name, output_dir)
        if path:
            return path

    if not lambda_function_name:
        _log.error(
            "Lambda function name not configured — set it in Settings > Commit0 Pipeline"
        )
        return None

    try:
        _log.info("Calling Lambda %s for %s", lambda_function_name, repo_name)
        pdf_bytes = _call_lambda_pdf(
            lambda_function_name,
            spec_url,
            repo_name,
            full_name,
            github_token,
            region=lambda_region,
            access_key=lambda_access_key,
            secret_key=lambda_secret_key,
        )
        if pdf_bytes and len(pdf_bytes) >= 500:
            pdf_path = os.path.join(output_dir, f"{repo_name}.pdf")
            _save_with_bz2(pdf_bytes, pdf_path)
            _log.info("Lambda PDF OK (%d bytes)", len(pdf_bytes))
            return pdf_path
        _log.warning("Lambda returned empty/small PDF for %s", repo_name)
    except Exception as exc:
        _log.error("Lambda PDF failed for %s: %s", repo_name, exc)

    return None
