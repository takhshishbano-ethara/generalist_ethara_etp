#!/usr/bin/env python3
"""Send a video clip plus the review.md system prompt to a Gemini model via OpenRouter.

This reads a local .mp4, base64-encodes it, extracts the verbatim system prompt
from sarvex/review.md, and sends both to OpenRouter (through LiteLLM) targeting
google/gemini-3.1-pro-preview. It retries once on a failed or partial generation,
appends all logs to a log file, and reports token usage and dollar cost.

Install
-------
    venv/bin/pip install -r requirements.txt
    # then put your key in the repo-root .env file:
    #   OPENROUTER_API_KEY=sk-or-...

Usage
-----
    python sarvex/send_review.py
    python sarvex/send_review.py --video CRW000078.mp4 --user-text-file sarvex/user_turn.txt
    python sarvex/send_review.py --out report.md --log-file run.log

Notes
-----
By default the system prompt is the fenced "SYSTEM PROMPT (verbatim)" block inside
review.md. Pass --raw-prompt to send the whole markdown file instead.

The review.md reviewer expects a user turn carrying ENRICHED_PROMPT, CATEGORY,
STYLE, PRIORITY, DURATION_SECONDS and RESOLUTION. Fill those in via --user-text
or --user-text-file; otherwise the reviewer will (correctly) return META-MISSING-INPUT.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import mimetypes
import os
import re
import sys
import time
from pathlib import Path

import litellm
from dotenv import load_dotenv

LOG = logging.getLogger("t2av.send_review")

litellm.suppress_debug_info = True

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent

DEFAULT_MODEL = "openrouter/google/gemini-3.1-pro-preview"
DEFAULT_VIDEO = _REPO_ROOT / "CRW000007_attempt_1.mp4"
DEFAULT_PROMPT = _HERE / "review.md"
DEFAULT_LOG_FILE = _HERE / "send_review.log"
ENV_FILE = _REPO_ROOT / ".env"

# Sampling guidance from review.md ("How to deploy"): near-deterministic judge.
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOP_P = 0.9
DEFAULT_REASONING_EFFORT = "high"
# gemini-3.x is a thinking model whose reasoning tokens count against max_tokens;
# with reasoning effort high the budget must be large or the report is truncated.
DEFAULT_MAX_TOKENS = 64000
DEFAULT_TIMEOUT = 3600
DEFAULT_MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 5.0

# Fallback pricing for the gemini-3.1 preview, applied only when neither OpenRouter
# nor LiteLLM's own price map reports a cost: 2.00 / 12.00 USD per 1M in/out tokens.
_FALLBACK_PRICING = {
    "openrouter/google/gemini-3.1-pro-preview": {
        "input_cost_per_token": 2e-06,
        "output_cost_per_token": 12e-06,
        "litellm_provider": "openrouter",
        "mode": "chat",
    }
}

DEFAULT_USER_TEXT = (
    "ENRICHED_PROMPT:\n"
    "<paste the exact final prompt string the generator received>\n"
    "\n"
    "CATEGORY: <av_sync_sound_effects | multi_speaker_dialogue | human_activities "
    "| high_motion_action | educational_videos>\n"
    "STYLE: <casual | precise | narrative | terse | exhaustive | creative>\n"
    "PRIORITY: <medium | high | highest>\n"
    "DURATION_SECONDS: <number>\n"
    "RESOLUTION: <e.g. 1920x1080>\n"
    "\n"
    "VIDEO: attached below."
)


def setup_logging(console_level: str, log_file: Path) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(fmt)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


def extract_system_prompt(markdown: str) -> str:
    marker = "## SYSTEM PROMPT (verbatim)"
    idx = markdown.find(marker)
    if idx == -1:
        LOG.warning("marker %r not found; using whole file as system prompt", marker)
        return markdown.strip()

    lines = markdown[idx + len(marker):].splitlines()

    open_at = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("```")), None
    )
    if open_at is None:
        LOG.warning("no opening fence after marker; using whole file")
        return markdown.strip()

    close_at = next(
        (i for i in range(open_at + 1, len(lines)) if lines[i].strip() == "```"),
        None,
    )
    if close_at is None:
        LOG.warning("no closing fence after marker; using whole file")
        return markdown.strip()

    return "\n".join(lines[open_at + 1:close_at]).strip()


def encode_video_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if not mime or not mime.startswith("video/"):
        mime = "video/mp4"
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    LOG.info(
        "encoded %s: %.2f MB -> %.2f MB base64 (%s)",
        path.name, len(raw) / 1e6, len(b64) / 1e6, mime,
    )
    return f"data:{mime};base64,{b64}"


def build_video_part(data_url: str, video_format: str) -> dict:
    if video_format == "file":
        return {"type": "file", "file": {"file_data": data_url}}
    return {"type": "video_url", "video_url": {"url": data_url}}


def normalize_model(model: str) -> str:
    return model if model.startswith("openrouter/") else f"openrouter/{model}"


def extract_cost(response: object) -> tuple[float, str]:
    hidden = getattr(response, "_hidden_params", None) or {}
    headers = hidden.get("additional_headers") or {}
    or_cost = headers.get("llm_provider-x-litellm-response-cost")
    if or_cost is not None:
        try:
            return float(or_cost), "openrouter"
        except (TypeError, ValueError):
            pass
    map_cost = hidden.get("response_cost")
    if map_cost:
        return float(map_cost), "litellm"
    return 0.0, "unknown"


def has_json_findings_block(text: str) -> bool:
    match = re.search(r"```json\s*(\{.*\})\s*```", text, re.S)
    if match is None:
        match = re.search(r"(\{.*\})\s*$", text, re.S)
    if match is None:
        return False
    try:
        json.loads(match.group(1))
    except json.JSONDecodeError:
        return False
    return True


def inspect_response(response: object, max_tokens: int) -> tuple[str, str | None]:
    if not isinstance(response, litellm.ModelResponse):
        return "", "LiteLLM returned a non-standard (streaming?) response"

    choice = response.choices[0]
    message = getattr(choice, "message", None)
    text = (getattr(message, "content", "") or "").strip()

    usage = getattr(response, "usage", None)
    total_tokens = getattr(usage, "total_tokens", 0) or 0

    if not text:
        return text, "empty response content"
    if total_tokens == 0:
        return text, "zero token usage; the provider aborted the generation"
    if getattr(choice, "finish_reason", None) == "length":
        return text, f"hit max_tokens={max_tokens}; report truncated"
    if not has_json_findings_block(text):
        return text, "missing a valid trailing JSON block"
    return text, None


def call_model(
    messages: list,
    model: str,
    api_key: str,
    max_tokens: int,
    timeout: int,
) -> object:
    return litellm.completion(
        model=model,
        messages=messages,
        api_key=api_key,
        temperature=DEFAULT_TEMPERATURE,
        top_p=DEFAULT_TOP_P,
        max_tokens=max_tokens,
        timeout=timeout,
        # num_retries/max_retries zero out LiteLLM's and the OpenAI client's hidden
        # retries so the explicit retry in review_with_retry is the only one.
        num_retries=0,
        max_retries=0,
        drop_params=True,
        extra_body={"reasoning": {"effort": DEFAULT_REASONING_EFFORT}},
    )


def review_with_retry(
    messages: list,
    model: str,
    api_key: str,
    max_tokens: int,
    timeout: int,
    max_attempts: int,
) -> tuple[object, str, str | None]:
    best: tuple[object, str, str | None] | None = None

    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        try:
            response = call_model(messages, model, api_key, max_tokens, timeout)
        except Exception as exc:
            LOG.warning(
                "attempt %d/%d: API call failed after %.0fs: %s",
                attempt, max_attempts, time.monotonic() - started, exc,
            )
            if attempt < max_attempts:
                time.sleep(RETRY_BACKOFF_SECONDS)
            continue

        text, problem = inspect_response(response, max_tokens)
        elapsed = time.monotonic() - started
        if problem is None:
            LOG.info(
                "attempt %d/%d: complete response in %.0fs", attempt, max_attempts, elapsed
            )
            return response, text, None

        LOG.warning(
            "attempt %d/%d: incomplete after %.0fs (%s)",
            attempt, max_attempts, elapsed, problem,
        )
        if best is None or len(text) > len(best[1]):
            best = (response, text, problem)
        if attempt < max_attempts:
            time.sleep(RETRY_BACKOFF_SECONDS)

    if best is not None:
        LOG.warning("all %d attempts incomplete; using the best partial result", max_attempts)
        return best
    raise SystemExit(f"review failed: no usable response after {max_attempts} attempts")


def review_video(
    video: Path,
    system_prompt: str,
    user_text: str,
    model: str,
    api_key: str,
    video_format: str,
    max_tokens: int,
    timeout: int,
    max_attempts: int,
) -> dict[str, object]:
    data_url = encode_video_data_url(video)
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                build_video_part(data_url, video_format),
            ],
        },
    ]

    LOG.info("calling %s via LiteLLM (video format=%s)", model, video_format)
    response, text, problem = review_with_retry(
        messages, model, api_key, max_tokens, timeout, max_attempts
    )

    usage = getattr(response, "usage", None)
    cost, cost_source = extract_cost(response)
    return {
        "text": text,
        "problem": problem,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "cost_usd": cost,
        "cost_source": cost_source,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="send_review.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--video", default=str(DEFAULT_VIDEO),
        help=f"path to the .mp4 clip (default: {DEFAULT_VIDEO})",
    )
    p.add_argument(
        "--prompt", default=str(DEFAULT_PROMPT),
        help=f"path to the prompt/review markdown file (default: {DEFAULT_PROMPT})",
    )
    p.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"model id; 'openrouter/' is added if missing (default: {DEFAULT_MODEL})",
    )
    p.add_argument(
        "--video-format", default="video_url", choices=["video_url", "file"],
        help="content-part shape for the video (default: video_url)",
    )
    p.add_argument(
        "--raw-prompt", action="store_true",
        help="send the whole prompt file as the system prompt instead of "
             "extracting the 'SYSTEM PROMPT (verbatim)' block",
    )
    p.add_argument(
        "--user-text", default=None,
        help="text for the user turn (defaults to an editable review.md input template)",
    )
    p.add_argument(
        "--user-text-file", default=None,
        help="read the user turn text from this file (overrides --user-text)",
    )
    p.add_argument(
        "--out", default=None,
        help="write the reviewer report to this file instead of stdout",
    )
    p.add_argument(
        "--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
        help=f"max completion tokens, includes reasoning tokens (default: {DEFAULT_MAX_TOKENS})",
    )
    p.add_argument(
        "--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
        help=f"attempts on a failed/partial generation (default: {DEFAULT_MAX_ATTEMPTS})",
    )
    p.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help="per-attempt httpx inter-byte timeout in seconds; not a hard wall-clock "
             f"cap, the run is intentionally uncapped (default: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--log-file", default=str(DEFAULT_LOG_FILE),
        help=f"append all DEBUG-level logs to this file (default: {DEFAULT_LOG_FILE})",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="console logging verbosity; the log file always gets DEBUG (default: INFO)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level, Path(args.log_file).expanduser())

    load_dotenv(ENV_FILE)
    litellm.register_model(_FALLBACK_PRICING)

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            f"OPENROUTER_API_KEY is empty. Add it to {ENV_FILE} as "
            "OPENROUTER_API_KEY=sk-or-..."
        )

    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        raise SystemExit(f"video not found: {video}")

    prompt_path = Path(args.prompt).expanduser().resolve()
    if not prompt_path.is_file():
        raise SystemExit(f"prompt file not found: {prompt_path}")

    markdown = prompt_path.read_text(encoding="utf-8")
    system_prompt = (
        markdown.strip() if args.raw_prompt else extract_system_prompt(markdown)
    )
    LOG.info(
        "system prompt: %d chars (%s)",
        len(system_prompt),
        "raw file" if args.raw_prompt else "verbatim block",
    )

    if args.user_text_file:
        user_path = Path(args.user_text_file).expanduser().resolve()
        if not user_path.is_file():
            raise SystemExit(f"user text file not found: {user_path}")
        user_text = user_path.read_text(encoding="utf-8")
    elif args.user_text is not None:
        user_text = args.user_text
    else:
        user_text = DEFAULT_USER_TEXT
        LOG.warning(
            "using the default user-turn template; the reviewer will likely return "
            "META-MISSING-INPUT until you supply real metadata via --user-text(-file)"
        )

    result = review_video(
        video=video,
        system_prompt=system_prompt,
        user_text=user_text,
        model=normalize_model(args.model),
        api_key=api_key,
        video_format=args.video_format,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        max_attempts=args.max_attempts,
    )

    report = str(result["text"])
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.write_text(report, encoding="utf-8")
        LOG.info("wrote report to %s", out_path)
    else:
        print(report)

    if result["problem"]:
        LOG.warning("NOTE: the report is partial/incomplete (%s)", result["problem"])

    LOG.info(
        "tokens: prompt=%s completion=%s total=%s | cost=$%.6f (source: %s)",
        result["prompt_tokens"],
        result["completion_tokens"],
        result["total_tokens"],
        result["cost_usd"],
        result["cost_source"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
