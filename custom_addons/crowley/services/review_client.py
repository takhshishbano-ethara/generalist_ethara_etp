from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

import requests

_logger = logging.getLogger(__name__)

_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REVIEW_MD_PATH = os.path.join(_MODULE_ROOT, "review.md")

DEFAULT_PROVIDER = "openrouter"
DEFAULT_BEDROCK_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"
DEFAULT_OPENROUTER_MODEL_ID = "moonshotai/kimi-k2-0905"
DEFAULT_REGION = "ap-south-1"
DEFAULT_MAX_TOKENS = 1500
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOP_P = 0.9
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_NUM_FRAMES = 20
DEFAULT_VIDEO_DOWNLOAD_TIMEOUT = 120

MAX_BEDROCK_VISION_IMAGES = 20
_MAX_VIDEO_BYTES = 200 * 1024 * 1024

_RETRYABLE_ERROR_NAMES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelTimeoutException",
    "ModelStreamErrorException",
    "RequestTimeout",
}

_FENCE_RE = re.compile(r"^```\s*$", re.MULTILINE)
_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
_SYSTEM_PROMPT_HEADER_RE = re.compile(
    r"^##\s*SYSTEM\s+PROMPT", re.MULTILINE | re.IGNORECASE,
)

_VALID_VERDICTS = {"ACCEPT", "REVIEW", "REJECT"}

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HTTP_REFERER = "https://crowley.local"
OPENROUTER_APP_TITLE = "Ethara Crowley Video Review"


class ReviewError(Exception):
    pass


class ReviewAuthError(ReviewError):
    pass


class ReviewConfigError(ReviewError):
    pass


class ReviewParseError(ReviewError):
    pass


_cached_review_prompt: str | None = None


def load_review_system_prompt() -> str:
    global _cached_review_prompt
    if _cached_review_prompt is not None:
        return _cached_review_prompt
    try:
        with open(_REVIEW_MD_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ReviewConfigError(
            f"Cannot read review.md at {_REVIEW_MD_PATH}: {e}"
        ) from e
    header_match = _SYSTEM_PROMPT_HEADER_RE.search(text)
    if not header_match:
        raise ReviewConfigError(
            f"Cannot find '## SYSTEM PROMPT' header in {_REVIEW_MD_PATH}."
        )
    fences = list(_FENCE_RE.finditer(text, header_match.end()))
    if len(fences) < 2:
        raise ReviewConfigError(
            f"Could not locate fenced system prompt block after "
            f"'## SYSTEM PROMPT' header in {_REVIEW_MD_PATH}."
        )
    start = fences[0].end()
    end = fences[1].start()
    block = text[start:end].strip("\n")
    if not block:
        raise ReviewConfigError(
            f"System prompt fenced block in {_REVIEW_MD_PATH} is empty."
        )
    _cached_review_prompt = block
    return block


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def download_video(url: str, dest_path: str, timeout: int = DEFAULT_VIDEO_DOWNLOAD_TIMEOUT) -> None:
    with requests.get(url, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and "video" not in ctype and "octet-stream" not in ctype and "binary" not in ctype:
            raise ReviewError(
                f"Refusing to download non-video content (Content-Type={ctype!r})."
            )
        cl = resp.headers.get("Content-Length")
        if cl is not None:
            try:
                if int(cl) > _MAX_VIDEO_BYTES:
                    raise ReviewError(
                        f"Video too large: Content-Length={cl} exceeds "
                        f"{_MAX_VIDEO_BYTES} bytes max."
                    )
            except ValueError:
                pass
        bytes_written = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                bytes_written += len(chunk)
                if bytes_written > _MAX_VIDEO_BYTES:
                    raise ReviewError(
                        f"Video exceeded {_MAX_VIDEO_BYTES} bytes during download."
                    )
                f.write(chunk)


def extract_frames(video_path: str, num_frames: int = DEFAULT_NUM_FRAMES) -> list[bytes]:
    if not _ffmpeg_available():
        raise ReviewConfigError(
            "ffmpeg is required for video review frame extraction. "
            "Install on the Odoo host (apt-get install ffmpeg or brew install ffmpeg)."
        )
    if num_frames < 1:
        raise ValueError("num_frames must be at least 1")
    with tempfile.TemporaryDirectory(prefix="crowley_review_") as tmpdir:
        out_pattern = os.path.join(tmpdir, "frame_%03d.jpg")
        try:
            duration_out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                check=True, capture_output=True, text=True, timeout=30,
            )
            duration = float(duration_out.stdout.strip())
        except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
            duration = 0.0
        if duration > 0:
            fps_value = max(0.01, num_frames / duration)
            fps_arg = f"fps={fps_value:.4f}"
        else:
            fps_arg = "fps=1"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", video_path,
                 "-vf", fps_arg, "-vframes", str(num_frames),
                 "-q:v", "2", out_pattern],
                check=True, capture_output=True, timeout=120,
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="ignore") if e.stderr else ""
            raise ReviewError(f"ffmpeg frame extraction failed: {stderr[:500]}") from e
        except subprocess.TimeoutExpired as e:
            raise ReviewError("ffmpeg frame extraction timed out (120s)") from e
        frames = []
        for fname in sorted(os.listdir(tmpdir)):
            if not fname.endswith(".jpg"):
                continue
            with open(os.path.join(tmpdir, fname), "rb") as f:
                frames.append(f.read())
        if not frames:
            raise ReviewError("ffmpeg extracted zero frames from the video")
        return frames[:num_frames]


def _is_retryable(exc: BaseException) -> bool:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
        if code in _RETRYABLE_ERROR_NAMES:
            return True
    name = exc.__class__.__name__
    if name in _RETRYABLE_ERROR_NAMES:
        return True
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _build_user_turn_with_frames(
    *,
    enriched_prompt: str,
    category: str,
    style: str,
    priority: str,
    duration_seconds: float,
    resolution: str,
    num_frames: int,
) -> str:
    return (
        f"ENRICHED_PROMPT:\n{enriched_prompt}\n\n"
        f"CATEGORY: {category}\n"
        f"STYLE: {style}\n"
        f"PRIORITY: {priority}\n"
        f"DURATION_SECONDS: {duration_seconds}\n"
        f"RESOLUTION: {resolution}\n\n"
        f"VIDEO: {num_frames} frames sampled below. The audio track is NOT "
        f"provided in this review pipeline. For PF-AUDIO-PRESENCE, "
        f"PF-AUDIO-SYNC, PF-DIALOGUE-MATCH (audio portion), TECH-AUDIO-SR, "
        f"GV-AUDIO-CLIPPING, GV-AUDIO-DROPOUT, GV-MUSIC-MASKING, "
        f"GV-AUDIO-SOURCE-MISMATCH, and GV-LIPSYNC-DRIFT: mark these rules "
        f"as N/A (not UNVERIFIABLE) because no audio was sent. If the frame "
        f"sampling rate is below 2 Hz for this clip, treat "
        f"META-INSUFFICIENT-FRAMES as N/A and proceed with the visual review "
        f"using the frames provided. Apply all PF-*, GV-* (non-audio), "
        f"TECH-RES, TECH-FPS, TECH-DURATION-BAND, and PC-* rules normally."
    )


def _build_user_turn_text_only(
    *,
    enriched_prompt: str,
    category: str,
    style: str,
    priority: str,
    duration_seconds: float,
    resolution: str,
    video_url: str,
) -> str:
    return (
        f"ENRICHED_PROMPT:\n{enriched_prompt}\n\n"
        f"CATEGORY: {category}\n"
        f"STYLE: {style}\n"
        f"PRIORITY: {priority}\n"
        f"DURATION_SECONDS: {duration_seconds}\n"
        f"RESOLUTION: {resolution}\n"
        f"VIDEO_URL: {video_url}\n\n"
        "REVIEW MODE: text-only. This pipeline uses a text-only LLM "
        "(no video frames are attached). Evaluate contract integrity, "
        "prompt quality, and producibility based on ENRICHED_PROMPT + metadata.\n\n"
        "Apply these rule-handling instructions:\n"
        "- GV-* (visual generative defects: hand morphology, identity drift, "
        "flicker, lip-sync, motion smear, contact incoherence, etc.): mark N/A. "
        "These require visual inspection.\n"
        "- PF-* (prompt fidelity): judge whether the ENRICHED_PROMPT is "
        "internally consistent, addresses each clause clearly, and contains "
        "all required elements (dynamic verb, surface, lighting, exactly one "
        "camera move, audio block with three sources, mandatory closing "
        "sentence). If a clause is ambiguous or missing, that's a FAIL.\n"
        "- TECH-* (resolution, fps, codec, audio sample rate, duration band): "
        "judge from the closing-sentence contract and DURATION_SECONDS. "
        "If the contract sentence matches spec (1920x1080 at 30 fps, "
        "48 kHz stereo or mono), PASS. If DURATION_SECONDS is outside "
        "8 to 25 seconds, FAIL TECH-DURATION-BAND.\n"
        "- PC-* (prohibited content: brand, celebrity, minor, unsafe, PII): "
        "judge from the prompt text only.\n"
        "- META-INSUFFICIENT-FRAMES: not applicable (no frames sent in this mode).\n\n"
        "Output the same prose + JSON contract defined in your system prompt. "
        "Verdict ACCEPT/REVIEW/REJECT based on the contract review above."
    )


def parse_review_output(text: str) -> dict[str, Any]:
    matches = list(_JSON_BLOCK_RE.finditer(text))
    if not matches:
        raise ReviewParseError(
            "No fenced ```json block found in reviewer output. "
            "Review.md output contract requires both prose and JSON."
        )
    parsed = None
    match = None
    last_decode_error = None
    for m in reversed(matches):
        candidate_str = m.group(1).strip()
        try:
            candidate = json.loads(candidate_str)
        except json.JSONDecodeError as e:
            last_decode_error = e
            continue
        if not isinstance(candidate, dict):
            continue
        if not {"verdict", "counts", "findings"}.issubset(candidate.keys()):
            continue
        parsed = candidate
        match = m
        break
    if parsed is None:
        raise ReviewParseError(
            "No ```json block contained the required schema "
            "(verdict + counts + findings). "
            f"Last JSON decode error: {last_decode_error}"
        )
    verdict = (parsed.get("verdict") or "").strip().upper()
    if verdict not in _VALID_VERDICTS:
        raise ReviewParseError(
            f"Reviewer returned invalid verdict {verdict!r}. "
            f"Expected one of {_VALID_VERDICTS}."
        )
    prose = text[: match.start()].strip()
    counts = parsed.get("counts") or {}
    rendered = parsed.get("rendered") or {}
    findings = parsed.get("findings") or []
    return {
        "verdict": verdict,
        "prose": prose,
        "findings": findings,
        "fatal_count": int(counts.get("fatal_fails") or 0),
        "major_count": int(counts.get("major_fails") or 0),
        "minor_count": int(counts.get("minor_fails") or 0),
        "unverifiable_count": int(counts.get("unverifiable") or 0),
        "regenerate_recommended": bool(parsed.get("regenerate_recommended")),
        "rebuilder_hint": parsed.get("rebuilder_hint") or "",
        "rendered_info": json.dumps(rendered, ensure_ascii=False),
        "raw_json": json.dumps(parsed, ensure_ascii=False),
    }


def _review_via_bedrock(
    *,
    access_key: str,
    secret_key: str,
    region: str,
    model_id: str,
    video_url: str,
    enriched_prompt: str,
    category: str,
    style: str,
    priority: str,
    duration_seconds: float,
    resolution: str,
    num_frames: int,
    max_tokens: int,
    temperature: float,
    top_p: float,
    max_attempts: int,
) -> dict[str, Any]:
    if not access_key or not secret_key:
        raise ReviewAuthError(
            "AWS Access Key ID and Secret Access Key required for Bedrock review. "
            "Configure in Settings > Crowley."
        )
    if num_frames < 1 or num_frames > MAX_BEDROCK_VISION_IMAGES:
        raise ReviewError(
            f"num_frames={num_frames} out of range for Bedrock vision "
            f"(1..{MAX_BEDROCK_VISION_IMAGES})."
        )
    try:
        import boto3
        from botocore.config import Config
    except ImportError as e:
        raise ReviewConfigError(
            "boto3 is required for Bedrock review. Install with: pip install boto3"
        ) from e
    system_prompt = load_review_system_prompt()
    with tempfile.NamedTemporaryFile(
        prefix="crowley_review_", suffix=".mp4", delete=False
    ) as tmp:
        local_path = tmp.name
    try:
        download_video(video_url, local_path)
        frames = extract_frames(local_path, num_frames=num_frames)
    finally:
        try:
            os.remove(local_path)
        except OSError:
            pass
    user_text = _build_user_turn_with_frames(
        enriched_prompt=enriched_prompt,
        category=category, style=style, priority=priority,
        duration_seconds=duration_seconds, resolution=resolution,
        num_frames=len(frames),
    )
    content_blocks: list[dict[str, Any]] = [{"text": user_text}]
    for frame_bytes in frames:
        content_blocks.append({
            "image": {"format": "jpeg", "source": {"bytes": frame_bytes}}
        })
    boto_config = Config(
        retries={"max_attempts": 0, "mode": "standard"},
        read_timeout=300,
        connect_timeout=10,
    )
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=boto_config,
    )
    last_exc: BaseException | None = None
    resp = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.converse(
                modelId=model_id,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": content_blocks}],
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                    "topP": top_p,
                },
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                raise ReviewError(f"{exc.__class__.__name__}: {exc}") from exc
            delay = min(60.0, (2 ** attempt) + random.random())
            _logger.warning(
                "Bedrock review retry %d/%d in %.1fs (%s: %s)",
                attempt, max_attempts, delay, exc.__class__.__name__, exc,
            )
            time.sleep(delay)
    if resp is None:
        raise ReviewError(
            f"Bedrock review call exhausted after {max_attempts} attempts: {last_exc}"
        )
    try:
        blocks = resp["output"]["message"]["content"]
    except (KeyError, TypeError) as e:
        raise ReviewError(f"Unexpected Bedrock response shape: {resp!r}") from e
    text_parts = [b.get("text", "") for b in blocks if "text" in b]
    text = "".join(text_parts).strip()
    if not text:
        raise ReviewError("Reviewer returned empty text.")
    parsed = parse_review_output(text)
    usage = resp.get("usage") or {}
    request_id = (resp.get("ResponseMetadata") or {}).get("RequestId", "")
    parsed["input_tokens"] = int(usage.get("inputTokens") or 0)
    parsed["output_tokens"] = int(usage.get("outputTokens") or 0)
    parsed["request_id"] = request_id
    parsed["num_frames"] = len(frames)
    parsed["provider"] = "bedrock"
    return parsed


def _review_via_openrouter(
    *,
    openrouter_api_key: str,
    model_id: str,
    video_url: str,
    enriched_prompt: str,
    category: str,
    style: str,
    priority: str,
    duration_seconds: float,
    resolution: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    max_attempts: int,
) -> dict[str, Any]:
    if not openrouter_api_key:
        raise ReviewAuthError(
            "OpenRouter API key required for OpenRouter review. "
            "Configure in Settings > Crowley > OpenRouter API Key."
        )
    system_prompt = load_review_system_prompt()
    user_text = _build_user_turn_text_only(
        enriched_prompt=enriched_prompt,
        category=category,
        style=style,
        priority=priority,
        duration_seconds=duration_seconds,
        resolution=resolution,
        video_url=video_url,
    )
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_HTTP_REFERER,
        "X-Title": OPENROUTER_APP_TITLE,
    }
    last_exc: BaseException | None = None
    response_json = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                OPENROUTER_API_URL,
                json=payload,
                headers=headers,
                timeout=300,
            )
            if resp.status_code == 401 or resp.status_code == 403:
                raise ReviewAuthError(
                    f"OpenRouter rejected credentials (HTTP {resp.status_code}): "
                    f"{resp.text[:200]}"
                )
            if resp.status_code >= 500 or resp.status_code == 429:
                if attempt >= max_attempts:
                    raise ReviewError(
                        f"OpenRouter HTTP {resp.status_code} after "
                        f"{max_attempts} attempts: {resp.text[:200]}"
                    )
                delay = min(60.0, (2 ** attempt) + random.random())
                _logger.warning(
                    "OpenRouter review retry %d/%d in %.1fs (HTTP %d)",
                    attempt, max_attempts, delay, resp.status_code,
                )
                time.sleep(delay)
                continue
            resp.raise_for_status()
            response_json = resp.json()
            break
        except ReviewAuthError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                raise ReviewError(f"{exc.__class__.__name__}: {exc}") from exc
            delay = min(60.0, (2 ** attempt) + random.random())
            _logger.warning(
                "OpenRouter review retry %d/%d in %.1fs (%s: %s)",
                attempt, max_attempts, delay, exc.__class__.__name__, exc,
            )
            time.sleep(delay)
    if response_json is None:
        raise ReviewError(
            f"OpenRouter review call exhausted after {max_attempts} attempts: {last_exc}"
        )
    try:
        text = response_json["choices"][0]["message"]["content"]
    except (KeyError, TypeError, IndexError) as e:
        raise ReviewError(f"Unexpected OpenRouter response shape: {response_json!r}") from e
    if not text or not text.strip():
        raise ReviewError("Reviewer returned empty text.")
    parsed = parse_review_output(text)
    usage = response_json.get("usage") or {}
    parsed["input_tokens"] = int(usage.get("prompt_tokens") or 0)
    parsed["output_tokens"] = int(usage.get("completion_tokens") or 0)
    parsed["request_id"] = response_json.get("id") or ""
    parsed["num_frames"] = 0
    parsed["provider"] = "openrouter"
    return parsed


def review(
    *,
    provider: str = DEFAULT_PROVIDER,
    access_key: str | None = None,
    secret_key: str | None = None,
    openrouter_api_key: str | None = None,
    region: str = DEFAULT_REGION,
    model_id: str | None = None,
    video_url: str,
    enriched_prompt: str,
    category: str,
    style: str,
    priority: str,
    duration_seconds: float,
    resolution: str,
    num_frames: int = DEFAULT_NUM_FRAMES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float = DEFAULT_TOP_P,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    if not video_url:
        raise ReviewError("video_url is required.")
    if not enriched_prompt:
        raise ReviewError("enriched_prompt is required.")
    provider_norm = (provider or "").strip().lower()
    if provider_norm == "bedrock":
        return _review_via_bedrock(
            access_key=access_key or "",
            secret_key=secret_key or "",
            region=region,
            model_id=model_id or DEFAULT_BEDROCK_MODEL_ID,
            video_url=video_url,
            enriched_prompt=enriched_prompt,
            category=category, style=style, priority=priority,
            duration_seconds=duration_seconds, resolution=resolution,
            num_frames=num_frames,
            max_tokens=max_tokens, temperature=temperature, top_p=top_p,
            max_attempts=max_attempts,
        )
    elif provider_norm == "openrouter":
        return _review_via_openrouter(
            openrouter_api_key=openrouter_api_key or "",
            model_id=model_id or DEFAULT_OPENROUTER_MODEL_ID,
            video_url=video_url,
            enriched_prompt=enriched_prompt,
            category=category, style=style, priority=priority,
            duration_seconds=duration_seconds, resolution=resolution,
            max_tokens=max_tokens, temperature=temperature, top_p=top_p,
            max_attempts=max_attempts,
        )
    else:
        raise ReviewConfigError(
            f"Unknown review provider {provider_norm!r}. "
            f"Expected 'bedrock' or 'openrouter'."
        )
