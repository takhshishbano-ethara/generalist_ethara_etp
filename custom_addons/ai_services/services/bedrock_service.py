import base64
import json
import logging
import re
from datetime import date
from urllib.parse import quote

import requests
from pydantic import ValidationError

from .schemas import ExtractionResponse, ProjectDetails
from .document_parser import extract_text_from_binary

_logger = logging.getLogger(__name__)

MODEL_NAME = "Kimi K2.5"

BEDROCK_CONVERSE_URL = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
)


def get_extraction_prompt():
    today_str = date.today().isoformat()
    return f"""You are a project-detail extraction assistant.

Analyse the uploaded document carefully and extract or infer the following fields.
If a value cannot be found explicitly in the document, make a smart, professional
inference based on the document context.

FIELD INSTRUCTIONS:
- client_project_name: The project name the client uses. Derive from document title/content.
- internal_project_name: Create a highly creative and unique internal codename based purely on the project's theme, scope, and objectives. DO NOT include the client's name or any reference to the client organisation. Do NOT include any version numbers. Do NOT reuse common words like "Quantum", "Nebula", "Phoenix", or "Atlas". The name must be original, unexpected, and memorable — something a branding agency would invent. Must be descriptive of the work itself.
- client_name: The official client organisation name found in the document.
- internal_client_name: A short alias or code for the client, e.g. "Ethara".
- project_category: One of: STEM, Non-STEM, Technical.
- project_type: Either "Single Turn" or "Multi Turn".
- start_date: Project start date in YYYY-MM-DD format. ONLY output this if it is explicitly stated in the document. Otherwise, return null. Do NOT assume today's date.
- end_date: Project end date in YYYY-MM-DD format. ONLY output this if explicitly stated in the document. Otherwise, return null.
- sample_task_number: A positive integer representing the number of sample tasks to produce. Infer based on document scope.
- description: A detailed project description summarizing scope and objectives.

RULES:
- Dates must be ISO-8601 (YYYY-MM-DD) or null. (Note: Today's date is {today_str}, but do not use it unless written in the file).
- sample_task_number must be a positive integer.
- internal_project_name must be unique and descriptive.
- Fill every field — do not leave any blank.

Return ONLY a valid JSON object with the fields above. No markdown formatting,
no code blocks, no additional text — just the raw JSON object."""


def _build_endpoint(region, inference_profile_arn):
    return BEDROCK_CONVERSE_URL.format(
        region=region,
        model_id=quote(inference_profile_arn, safe=""),
    )


def _extract_text_from_documents(documents):
    parts = []
    for filename, file_bytes in documents:
        b64_data = base64.b64encode(file_bytes)
        try:
            text = extract_text_from_binary(b64_data, filename)
            parts.append(f"--- Document: {filename} ---\n{text}")
        except ValueError as e:
            _logger.warning("Skipping %s: %s", filename, e)
            parts.append(f"--- Document: {filename} ---\n[Could not extract: {e}]")
    return "\n\n".join(parts)


def call_bedrock_extract(
    documents, api_key, inference_profile_arn, region="ap-south-1"
):
    """Call AWS Bedrock Converse API with documents for extraction.

    Extracts text from each document locally, then sends combined text
    to the LLM (Kimi K2.5 does not support document content blocks).

    Args:
        documents: list of (filename, file_bytes) tuples — raw file content.
        api_key: Bedrock API key.
        inference_profile_arn: Full ARN of the inference profile.
        region: AWS region.

    Returns:
        ProjectDetails: Pydantic-validated extraction result.
    """
    combined_text = _extract_text_from_documents(documents)
    if not combined_text.strip():
        raise ValueError("No text could be extracted from the uploaded documents.")

    url = _build_endpoint(region, inference_profile_arn)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            "Extract the project details from the following "
                            "document content:\n\n" + combined_text
                        ),
                    },
                ],
            },
        ],
        "system": [
            {"text": get_extraction_prompt()},
        ],
        "inferenceConfig": {
            "maxTokens": 4096,
            "temperature": 0.7,
            "topP": 0.9,
        },
        "additionalModelRequestFields": {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ProjectDetails",
                    "schema": ProjectDetails.model_json_schema(),
                },
            },
        },
    }

    doc_names = [name for name, _ in documents]
    _logger.info(
        "Calling Bedrock Converse API (%s) with %d document(s): %s",
        region,
        len(documents),
        doc_names,
    )

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=120)
    except requests.exceptions.ConnectionError:
        raise ConnectionError(
            "Cannot reach AWS Bedrock API. Check your internet connection."
        )
    except requests.exceptions.Timeout:
        raise ConnectionError("Bedrock API request timed out after 120 seconds.")

    if response.status_code != 200:
        error_detail = response.text[:500]
        _logger.error(
            "Bedrock API returned status %d: %s", response.status_code, error_detail
        )
        raise RuntimeError(
            f"Bedrock API error (HTTP {response.status_code}): {error_detail}"
        )

    result = response.json()

    # Check for Coral-level errors (e.g. UnknownOperationException)
    output_key = "output" if "output" in result else "Output"
    if output_key in result and isinstance(result[output_key], dict):
        err_type = result[output_key].get("__type", "")
        if err_type:
            raise RuntimeError(f"Bedrock API service error: {err_type}")

    response_text = _extract_response_text(result)
    _logger.info("Bedrock response received (%d chars)", len(response_text))

    raw_dict = _parse_json_response(response_text)

    try:
        return ProjectDetails.model_validate(raw_dict)
    except ValidationError as e:
        _logger.error("Pydantic validation failed: %s", e)
        raise ValueError(f"LLM output failed schema validation: {e}")


def _extract_response_text(api_response):
    try:
        content = api_response["output"]["message"]["content"]
        for block in content:
            if "text" in block:
                return block["text"]
        raise ValueError("No text content in Bedrock response")
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"Unexpected Bedrock response structure: {e}")


def _parse_json_response(response_text):
    cleaned = response_text.strip()

    json_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
    if json_block_match:
        cleaned = json_block_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    brace_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"Could not parse JSON from LLM response. Raw text: {response_text[:300]}"
    )


def build_extraction_response(
    filename, project_details: ProjectDetails
) -> ExtractionResponse:
    return ExtractionResponse(
        filename=filename,
        model_name=MODEL_NAME,
        project_details=project_details,
    )
