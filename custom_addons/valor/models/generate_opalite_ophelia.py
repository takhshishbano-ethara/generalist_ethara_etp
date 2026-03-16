#!/usr/bin/env python3
"""
Generate Response A and Response B via Meta GenAI API using fixed model names:
  - Response A: opalite
  - Response B: ophelia

Both API calls are run in parallel. Supports multi-turn: pass dialog_history
(previous user/assistant pairs; assistant content is always from the caller, never
from our response_a/response_b) and reuse dialog_id across turns (provided once).

Public API:
  - get_dialog_id(genai_api_key)     Config: call once to get dialog_id for the first turn.
  - generate_response_a_and_b(...)  Single entry point: single-turn or multi-turn (pass optional dialog_history and dialog_id).

Usage:
  cd odoo-19/ethara_addons/valor
  python3 scripts/generate_opalite_ophelia.py "Your prompt here"
  python3 scripts/generate_opalite_ophelia.py --prompt-file prompt.txt
  python3 scripts/generate_opalite_ophelia.py "Turn 2 prompt" --dialog-history history.json --dialog-id <id>
  echo "Your prompt" | python3 scripts/generate_opalite_ophelia.py

Requires: genai_api_key or GENAI_ACCESS_TOKEN in .env or environment.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

try:
    import requests
except ImportError:
    print("This script requires 'requests'. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

# Addon root for .env loading
_addon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _addon_root not in sys.path:
    sys.path.insert(0, _addon_root)

try:
    from dotenv import load_dotenv
    _env_path = os.path.join(_addon_root, ".env")
    if os.path.isfile(_env_path):
        load_dotenv(_env_path)
except ImportError:
    pass


GRAPH_BASE_URL = "https://graph-genai.facebook.com/v24.0"
WORKSTREAM = "vendor_onboarding"
MODEL_RESPONSE_A = "opalite"
MODEL_RESPONSE_B = "ophelia"


def get_genai_api_key():
    """Read GenAI access token from env (genai_api_key or GENAI_ACCESS_TOKEN)."""
    return (os.environ.get("genai_api_key") or os.environ.get("GENAI_ACCESS_TOKEN") or "").strip()


def get_router_config(genai_api_key: str) -> dict:
    """
    Call Meta config API (llm_annotations_model_router_workstream). Returns raw
    router response. Model names from config are ignored; we use MODEL_RESPONSE_A
    and MODEL_RESPONSE_B.
    """
    url = f"{GRAPH_BASE_URL}/llm_annotations_model_router_workstream"
    payload = {
        "access_token": genai_api_key,
        "workstream": WORKSTREAM,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_dialog_id(genai_api_key: str) -> str:
    """
    Separate config step: call Meta config API and return dialog_id for generation.
    Call once before the first turn; reuse the returned dialog_id for all later turns.
    """
    router = get_router_config(genai_api_key)
    return (router.get("dialog_id") or "").strip() or ""


def upload_attachment(
    genai_api_key: str,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
) -> dict:
    """
    Upload a file to Meta GenAI attachment API; returns dict with handle_id.
    Uses multipart/form-data: file= and access_token=.
    """
    url = f"{GRAPH_BASE_URL}/llm_annotations_attachment_upload"
    files = {"file": (filename, file_bytes, mime_type or "application/octet-stream")}
    data = {"access_token": genai_api_key}
    resp = requests.post(url, files=files, data=data, timeout=60)
    resp.raise_for_status()
    try:
        out = resp.json()
    except Exception:
        out = {}
    handle_id = (out.get("handle_id") or "").strip() if isinstance(out, dict) else ""
    if not handle_id and isinstance(out, dict):
        for k, v in out.items():
            if "handle" in k.lower() and v:
                handle_id = str(v)
                break
    return {"handle_id": handle_id, "mime": mime_type or "image/png"}


def _build_message(role: str, text: str = None, attachment_handle_id: str = None, attachment_mime: str = None) -> List[dict]:
    """
    Build one or two message dicts for Meta API (role 'user' or 'assistant').
    If attachment_handle_id is set, returns [attachment_msg] (and optionally [attachment_msg, text_msg] if text).
    If only text, returns [text_msg]. Assistant messages are always text-only.
    """
    out = []
    if attachment_handle_id and role == "user":
        out.append({
            "source": {"role": "user"},
            "contents": [{"attachment": {"handle_id": attachment_handle_id, "mime": attachment_mime or "image/png"}}],
            "is_end_of_turn": True,
            "is_complete": True,
        })
    if text is not None:
        out.append({
            "source": {"role": role},
            "contents": [{"text": {"text": text}}],
            "is_end_of_turn": True,
            "is_complete": True,
        })
    return out if out else [{
        "source": {"role": role},
        "contents": [{"text": {"text": text or ""}}],
        "is_end_of_turn": True,
        "is_complete": True,
    }]


def _build_messages_from_history(
    dialog_history: Optional[List[Tuple[str, str]]],
    current_prompt: str,
    current_turn_handle_id: Optional[str] = None,
    current_turn_mime: Optional[str] = None,
    history_handle_ids: Optional[List[Tuple[Optional[str], Optional[str]]]] = None,
) -> List[dict]:
    """
    Build the full messages list for the API: all previous turns (user + assistant
    from caller) then the current user message. When a turn has an image, emit
    user attachment message then user text message then assistant (for history).
    current_turn_handle_id/current_turn_mime: optional image for current turn.
    history_handle_ids: optional list of (handle_id, mime) per history turn, same length as dialog_history.
    """
    messages = []
    if dialog_history:
        for i, (user_text, assistant_text) in enumerate(dialog_history):
            h_handle, h_mime = (history_handle_ids[i] if history_handle_ids and i < len(history_handle_ids) else (None, None)) or (None, None)
            if h_handle:
                for msg in _build_message("user", user_text, attachment_handle_id=h_handle, attachment_mime=h_mime):
                    messages.append(msg)
            else:
                messages.extend(_build_message("user", user_text))
            messages.extend(_build_message("assistant", assistant_text or ""))
    if current_turn_handle_id:
        for msg in _build_message("user", current_prompt, attachment_handle_id=current_turn_handle_id, attachment_mime=current_turn_mime):
            messages.append(msg)
    else:
        messages.extend(_build_message("user", current_prompt))
    return messages


def _call_generation(model_name: str, dialog_id: str, messages: List[dict], genai_api_key: str) -> dict:
    """
    Single Meta GenAI generation call. Returns the raw response dict containing
    dialog_candidates (streamed response: we parse the last line with dialog_candidates).
    messages: list of message dicts (from _build_message / _build_messages_from_history).
    """
    url = f"{GRAPH_BASE_URL}/llm_annotations_metagen_stream_turn"
    payload = {
        "access_token": genai_api_key,
        "dialog": {"messages": messages},
        "workstream": WORKSTREAM,
        "model": model_name,
        "dialog_id": dialog_id,
    }
    response = requests.post(url, json=payload, timeout=60)
    response.raise_for_status()

    data = None
    for line in reversed(response.text.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if "dialog_candidates" in parsed:
                data = parsed
                break
        except json.JSONDecodeError:
            continue

    if data is None:
        raise ValueError("No valid response with dialog_candidates found from GenAI API")
    return data


def _extract_text_from_response(data: dict) -> str:
    """
    Extract the new generated text from Meta API response (dialog_candidates path).
    Uses the last message in the dialog (the new assistant reply); works for both
    single-turn and multi-turn (history + new reply).
    """
    if not data or "dialog_candidates" not in data or not data["dialog_candidates"]:
        return ""
    cand = data["dialog_candidates"][0]
    dialog = cand.get("dialog") or {}
    messages = dialog.get("messages") or []
    if not messages:
        return ""
    last_msg = messages[-1]
    contents = last_msg.get("contents") or []
    if not contents:
        return ""
    text_obj = contents[0].get("text") or {}
    return text_obj.get("text") or ""


def _generate_one(
    model_name: str,
    dialog_id: str,
    prompt: str,
    genai_api_key: str,
    dialog_history: Optional[List[Tuple[str, str]]],
    current_turn_handle_id: Optional[str] = None,
    current_turn_mime: Optional[str] = None,
    history_handle_ids: Optional[List[Tuple[Optional[str], Optional[str]]]] = None,
) -> str:
    """Build messages from dialog_history + prompt, call Meta API for one model, return generated text. Internal use only."""
    messages = _build_messages_from_history(
        dialog_history,
        prompt,
        current_turn_handle_id=current_turn_handle_id,
        current_turn_mime=current_turn_mime,
        history_handle_ids=history_handle_ids,
    )
    raw = _call_generation(model_name, dialog_id, messages, genai_api_key)
    return _extract_text_from_response(raw)


def generate_response_a_and_b(
    prompt: str,
    genai_api_key: str,
    *,
    dialog_id: Optional[str] = None,
    dialog_history: Optional[List[Tuple[str, str]]] = None,
    current_turn_handle_id: Optional[str] = None,
    current_turn_mime: Optional[str] = None,
    history_handle_ids: Optional[List[Tuple[Optional[str], Optional[str]]]] = None,
) -> dict:
    """
    Generate Response A (opalite) and Response B (ophelia) in parallel.

    dialog_id is provided or fetched once; reuse the returned dialog_id for all
    subsequent turns. Assistant content in dialog_history is always from the
    caller (never from our previous response_a/response_b).
    Optional image for current turn and per history turn via handle_id/mime.

    Args:
        prompt: Current user prompt text for this turn.
        genai_api_key: Meta GenAI access token.
        dialog_id: Optional. If provided, use it (caller should provide once and
            reuse for all turns). If not provided, fetch from router once.
        dialog_history: Optional. List of (user_text, assistant_text) for previous
            turns. Assistant text is supplied by the caller (from elsewhere).
        current_turn_handle_id: Optional. Facebook attachment handle_id for current turn.
        current_turn_mime: Optional. MIME for current turn image (e.g. image/png).
        history_handle_ids: Optional. List of (handle_id, mime) per history turn.

    Returns:
        {
            "response_a": str,
            "response_b": str,
            "dialog_id": str,
            "model_a": "opalite",
            "model_b": "ophelia",
            "errors": dict (optional),
        }
    """
    if not genai_api_key:
        raise ValueError("genai_api_key is required")
    prompt = (prompt or "").strip()
    if not prompt and not current_turn_handle_id:
        raise ValueError("prompt or current_turn_handle_id is required")

    if not dialog_id:
        dialog_id = get_dialog_id(genai_api_key)

    result = {"response_a": "", "response_b": "", "dialog_id": dialog_id, "model_a": MODEL_RESPONSE_A, "model_b": MODEL_RESPONSE_B}
    errors = {}

    def run_a():
        return _generate_one(
            MODEL_RESPONSE_A,
            dialog_id,
            prompt,
            genai_api_key,
            dialog_history,
            current_turn_handle_id=current_turn_handle_id,
            current_turn_mime=current_turn_mime,
            history_handle_ids=history_handle_ids,
        )

    def run_b():
        return _generate_one(
            MODEL_RESPONSE_B,
            dialog_id,
            prompt,
            genai_api_key,
            dialog_history,
            current_turn_handle_id=current_turn_handle_id,
            current_turn_mime=current_turn_mime,
            history_handle_ids=history_handle_ids,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_key = {
            executor.submit(run_a): "a",
            executor.submit(run_b): "b",
        }
        for fut in as_completed(future_to_key):
            key = future_to_key[fut]
            try:
                text = fut.result()
                result["response_a" if key == "a" else "response_b"] = text or ""
            except Exception as e:
                errors[key] = str(e)
                result["response_a" if key == "a" else "response_b"] = ""

    if errors:
        result["errors"] = errors
    return result