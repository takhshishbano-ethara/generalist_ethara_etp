#!/usr/bin/env python3
"""
Generate Ophelia and Opalite responses via Meta GenAI API, extract only the
reasoning blocks, and write them back to the source CSV.

Input CSV columns: Employee, Task, Enhance Prompt, Ophelia Response A, Opalite Response B
Output: Same CSV with four NEW columns:
        "Reasoning A", "Response A", "Reasoning B", "Response B"
        Reasoning columns contain content between |<reasoning_start>| and
        |<reasoning_end>| tags. Response columns contain the remaining text
        with reasoning tags stripped. Original response columns are preserved.

Usage:
    cd odoo-18/custom_addons/preference_ranking
    python scripts/generate_reasoning.py data.csv
    python scripts/generate_reasoning.py data.csv --output results.csv
    python scripts/generate_reasoning.py data.csv --workers 4
    python scripts/generate_reasoning.py data.csv --dry-run          # show what would be processed

Requires:
    - GENAI_ACCESS_TOKEN or genai_api_key environment variable (or in .env)
    - pip install requests python-dotenv
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print(
        "Missing dependency: requests\nInstall with: pip install requests",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_ROOT = os.path.dirname(_SCRIPT_DIR)
_ADDONS_ROOT = os.path.dirname(_MODULE_ROOT)

try:
    from dotenv import load_dotenv

    for candidate in [_MODULE_ROOT, _ADDONS_ROOT]:
        env_file = os.path.join(candidate, ".env")
        if os.path.isfile(env_file):
            load_dotenv(env_file)
            break
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("generate_reasoning")

# ---------------------------------------------------------------------------
# Meta GenAI constants (matching preference_ranking/controllers/llm_actions.py)
# ---------------------------------------------------------------------------
GRAPH_BASE_URL = "https://graph-genai.facebook.com/v24.0"
WORKSTREAM_OPALITE = "opalite"
WORKSTREAM_OPHELIA = "ophelia"
MODEL_OPALITE = "opalite"
MODEL_OPHELIA = "ophelia"

MAX_RETRIES = 4
BACKOFF_BASE = 2.0
RETRY_STATUS_CODES = {500, 502, 503, 504}

# CSV column names
COL_EMPLOYEE = "Employee"
COL_TASK = "Task"
COL_PROMPT = "Enhance Prompt"
COL_OPHELIA = "Ophelia Response A"
COL_OPALITE = "Opalite Response B"
COL_REASONING_A = "Reasoning A"
COL_RESPONSE_A = "Response A"
COL_REASONING_B = "Reasoning B"
COL_RESPONSE_B = "Response B"

REQUIRED_COLUMNS = [COL_EMPLOYEE, COL_TASK, COL_PROMPT, COL_OPHELIA, COL_OPALITE]
OUTPUT_COLUMNS = REQUIRED_COLUMNS + [
    COL_REASONING_A,
    COL_RESPONSE_A,
    COL_REASONING_B,
    COL_RESPONSE_B,
]

# Reasoning tag pattern
_REASONING_RE = re.compile(r"\|<reasoning_start>\|(.*?)\|<reasoning_end>\|", re.DOTALL)


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[502, 503, 504],
        allowed_methods=["POST"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=6, pool_maxsize=12)
    session.mount("https://", adapter)
    session.headers.update(
        {"Content-Type": "application/json", "Accept": "application/json"}
    )
    return session


_SESSION = _build_session()


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def _post(url: str, payload: dict, label: str = "API") -> requests.Response:
    """POST with application-level retry on 5xx errors."""
    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        resp = _SESSION.post(url, json=payload)
        if resp.status_code not in RETRY_STATUS_CODES:
            break
        log.warning(
            "[%s] Attempt %d/%d HTTP %d",
            label,
            attempt,
            MAX_RETRIES,
            resp.status_code,
        )
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF_BASE**attempt)

    if resp is not None and resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500]
        log.error("[%s] HTTP %d: %s", label, resp.status_code, body)
        resp.raise_for_status()
    return resp


def get_api_key() -> str:
    """Read GenAI access token from environment."""
    key = (
        os.environ.get("genai_api_key") or os.environ.get("GENAI_ACCESS_TOKEN") or ""
    ).strip()
    if not key:
        log.error(
            "No API key found. Set GENAI_ACCESS_TOKEN or genai_api_key in "
            "environment or .env file."
        )
        sys.exit(1)
    return key


def get_dialog_id(api_key: str, workstream: str) -> str:
    """Fetch dialog_id from Meta router config API for a given workstream."""
    url = f"{GRAPH_BASE_URL}/llm_annotations_model_router_workstream"
    payload = {"access_token": api_key, "workstream": workstream}
    resp = _post(url, payload, label=f"RouterConfig({workstream})")
    return (resp.json().get("dialog_id") or "").strip()


# ---------------------------------------------------------------------------
# Generation + reasoning extraction
# ---------------------------------------------------------------------------
def _call_generation(
    model: str,
    workstream: str,
    dialog_id: str,
    prompt: str,
    api_key: str,
) -> str:
    """Call Meta GenAI generation endpoint and return the raw response text
    (including reasoning tags, before any stripping)."""
    url = f"{GRAPH_BASE_URL}/llm_annotations_metagen_stream_turn"
    messages = [
        {
            "source": {"role": "user"},
            "contents": [{"text": {"text": prompt}}],
            "is_end_of_turn": True,
            "is_complete": True,
        }
    ]
    payload = {
        "access_token": api_key,
        "dialog": {"messages": messages},
        "workstream": workstream,
        "model": model,
        "dialog_id": dialog_id,
        "options": {"max_tokens": 50000},
    }
    resp = _post(url, payload, label=f"Generate({model})")

    # Parse streamed response — last JSON line with dialog_candidates
    data = None
    for line in reversed(resp.text.strip().splitlines()):
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
        raise ValueError(f"No dialog_candidates in {model} response")

    # Extract raw text (DO NOT strip reasoning — we need it)
    cand = data["dialog_candidates"][0]
    msgs = (cand.get("dialog") or {}).get("messages") or []
    if not msgs:
        return ""
    contents = msgs[-1].get("contents") or []
    if not contents:
        return ""
    return (contents[0].get("text") or {}).get("text") or ""


def _split_reasoning_response(raw: str) -> tuple:
    """Split raw LLM output into (reasoning, response) using
    |<reasoning_start>| / |<reasoning_end>| tags."""
    match = _REASONING_RE.search(raw)
    if not match:
        return "", raw.strip()

    reasoning = match.group(1).strip()
    response = raw[: match.start()] + raw[match.end() :]
    response = re.sub(r"\n{3,}", "\n\n", response).strip()
    return reasoning, response


def generate_for_prompt(
    prompt: str,
    api_key: str,
    opalite_dialog_id: str,
    ophelia_dialog_id: str,
) -> dict:
    result = {
        "ophelia_reasoning": "",
        "ophelia_response": "",
        "opalite_reasoning": "",
        "opalite_response": "",
        "errors": [],
    }

    def run_ophelia():
        raw = _call_generation(
            MODEL_OPHELIA, WORKSTREAM_OPHELIA, ophelia_dialog_id, prompt, api_key
        )
        return _split_reasoning_response(raw)

    def run_opalite():
        raw = _call_generation(
            MODEL_OPALITE, WORKSTREAM_OPALITE, opalite_dialog_id, prompt, api_key
        )
        return _split_reasoning_response(raw)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(run_ophelia): "ophelia",
            pool.submit(run_opalite): "opalite",
        }

        for fut in as_completed(futures):
            model = futures[fut]
            try:
                reasoning, response = fut.result()
                result["{}_reasoning".format(model)] = reasoning
                result["{}_response".format(model)] = response
            except Exception as exc:
                result["errors"].append("{}: {}".format(model, exc))
                log.error("Failed %s generation: %s", model, exc)

    return result


# ---------------------------------------------------------------------------
# CSV processing
# ---------------------------------------------------------------------------
def process_csv(
    input_path: str,
    output_path: str,
    max_workers: int,
    dry_run: bool,
    skip_existing: bool,
) -> None:
    """Read CSV, generate reasoning for each prompt, write results."""
    api_key = get_api_key()

    # Read input
    with open(input_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            log.error("CSV file is empty or has no headers.")
            sys.exit(1)

        # Validate columns
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            log.error(
                "CSV missing required columns: %s\nFound: %s",
                missing,
                reader.fieldnames,
            )
            sys.exit(1)

        rows = list(reader)

    total = len(rows)
    log.info("Loaded %d rows from %s", total, input_path)

    if dry_run:
        for i, row in enumerate(rows):
            prompt = (row.get(COL_PROMPT) or "").strip()
            status = "SKIP (empty)" if not prompt else "PROCESS"
            if skip_existing and row.get(COL_REASONING_A, "").strip():
                status = "SKIP (existing)"
            log.info("  [%d/%d] %s — %.60s...", i + 1, total, status, prompt)
        log.info("Dry run complete. No API calls made.")
        return

    # Fetch dialog IDs (one per workstream, reused for all prompts)
    log.info("Fetching dialog IDs...")
    opalite_dialog_id = get_dialog_id(api_key, WORKSTREAM_OPALITE)
    ophelia_dialog_id = get_dialog_id(api_key, WORKSTREAM_OPHELIA)
    log.info(
        "Dialog IDs — opalite: %.20s..., ophelia: %.20s...",
        opalite_dialog_id,
        ophelia_dialog_id,
    )

    # Write initial output (preserves data if script crashes early)
    _write_output(output_path, rows)

    # Process rows concurrently
    completed = 0
    failed = 0

    def process_row(index: int, row: dict) -> tuple:
        prompt = (row.get(COL_PROMPT) or "").strip()
        if not prompt:
            return index, row, "skipped_empty"
        if skip_existing and row.get(COL_REASONING_A, "").strip():
            return index, row, "skipped_existing"

        result = generate_for_prompt(
            prompt, api_key, opalite_dialog_id, ophelia_dialog_id
        )
        row[COL_REASONING_A] = result["ophelia_reasoning"]
        row[COL_RESPONSE_A] = result["ophelia_response"]
        row[COL_REASONING_B] = result["opalite_reasoning"]
        row[COL_RESPONSE_B] = result["opalite_response"]
        if result["errors"]:
            return index, row, f"partial: {'; '.join(result['errors'])}"
        return index, row, "ok"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(process_row, i, row): i for i, row in enumerate(rows)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                index, row, status = fut.result()
                rows[index] = row
                completed += 1
                # Save after each row so progress is not lost on crash
                _write_output(output_path, rows)
                if status == "ok":
                    log.info(
                        "[%d/%d] Row %d — done (saved)", completed, total, index + 1
                    )
                elif status.startswith("partial"):
                    log.warning(
                        "[%d/%d] Row %d — %s", completed, total, index + 1, status
                    )
                    failed += 1
                else:
                    log.info("[%d/%d] Row %d — %s", completed, total, index + 1, status)
            except Exception as exc:
                completed += 1
                failed += 1
                log.error("[%d/%d] Row %d — error: %s", completed, total, idx + 1, exc)

    log.info(
        "Done. %d/%d processed, %d failed. Output: %s",
        completed,
        total,
        failed,
        output_path,
    )


def _write_output(path: str, rows: list) -> None:
    """Write current row state to CSV (called after each row for crash safety)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=OUTPUT_COLUMNS, restval="", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=(
            "Generate Ophelia/Opalite responses via Meta GenAI and extract "
            "reasoning to CSV."
        ),
    )
    parser.add_argument("csv_file", help="Input CSV file path")
    parser.add_argument(
        "-o",
        "--output",
        help="Output CSV path (default: overwrites input file)",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=3,
        help="Max concurrent prompts to process (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without making API calls",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip rows that already have Reasoning A populated",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not os.path.isfile(args.csv_file):
        log.error("File not found: %s", args.csv_file)
        sys.exit(1)

    output = args.output or args.csv_file
    process_csv(args.csv_file, output, args.workers, args.dry_run, args.skip_existing)


if __name__ == "__main__":
    main()
