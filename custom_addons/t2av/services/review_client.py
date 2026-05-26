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
DEFAULT_OPENROUTER_MODEL_ID = "google/gemini-3.1-pro-preview"
DEFAULT_REGION = "ap-south-1"
DEFAULT_BEDROCK_MAX_TOKENS = 1500
DEFAULT_OPENROUTER_MAX_TOKENS = 64000
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOP_P = 0.9
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_NUM_FRAMES = 20
DEFAULT_VIDEO_DOWNLOAD_TIMEOUT = 120
DEFAULT_REVIEW_VIDEO_TTL_SECONDS = 900
DEFAULT_OPENROUTER_TIMEOUT = 3600

MAX_BEDROCK_VISION_IMAGES = 20
_MAX_VIDEO_BYTES = 200 * 1024 * 1024
_MAX_PREVIOUS_FAILURES = 5
_URL_FETCH_ERROR_HINTS = (
    "fetch", "download", "media", "url", "unreachable", "timed out",
    "not accessible", "invalid url", "could not retrieve",
)

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
OPENROUTER_HTTP_REFERER = "https://t2av.local"
OPENROUTER_APP_TITLE = "Ethara T2AV Video Review"


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
    with tempfile.TemporaryDirectory(prefix="t2av_review_") as tmpdir:
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


def _format_previous_failures(previous_failures: list[dict] | None) -> str:
    if not previous_failures:
        return ""
    lines = ["PREVIOUS_ATTEMPT_FAILURES:"]
    for finding in previous_failures[:_MAX_PREVIOUS_FAILURES]:
        rule = (finding.get("rule") or "UNKNOWN").strip()
        severity = (finding.get("severity") or "").strip().upper()
        evidence = (finding.get("evidence") or "").strip()
        if len(evidence) > 240:
            evidence = evidence[:237] + "..."
        prefix = f"[{rule}]"
        if severity:
            prefix = f"[{rule} / {severity}]"
        if evidence:
            lines.append(f"- {prefix} {evidence}")
        else:
            lines.append(f"- {prefix}")
    lines.append(
        "Address each failure above. Re-evaluate the current video against "
        "every rule the prior attempt failed; do not assume any of them "
        "were fixed."
    )
    return "\n".join(lines)


def _build_review_user_text(
    *,
    enriched_prompt: str,
    category: str,
    style: str,
    priority: str,
    duration_seconds: float,
    resolution: str,
    previous_failures: list[dict] | None = None,
) -> str:
    body = (
        f"ENRICHED_PROMPT:\n{enriched_prompt}\n\n"
        f"CATEGORY: {category}\n"
        f"STYLE: {style}\n"
        f"PRIORITY: {priority}\n"
        f"DURATION_SECONDS: {duration_seconds}\n"
        f"RESOLUTION: {resolution}\n\n"
        "VIDEO: attached below."
    )
    failures_block = _format_previous_failures(previous_failures)
    if failures_block:
        body = f"{body}\n\n{failures_block}"
    return body


def _encode_video_data_url(video_bytes: bytes, mime: str = "video/mp4") -> str:
    import base64
    b64 = base64.b64encode(video_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _download_video_bytes(url: str, timeout: int = DEFAULT_VIDEO_DOWNLOAD_TIMEOUT) -> bytes:
    with requests.get(url, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        cl = resp.headers.get("Content-Length")
        if cl is not None:
            try:
                if int(cl) > _MAX_VIDEO_BYTES:
                    raise ReviewError(
                        f"Video too large for base64 fallback: Content-Length={cl} "
                        f"exceeds {_MAX_VIDEO_BYTES} bytes max."
                    )
            except ValueError:
                pass
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > _MAX_VIDEO_BYTES:
                raise ReviewError(
                    f"Video exceeded {_MAX_VIDEO_BYTES} bytes during download."
                )
        return bytes(buf)


def _build_multimodal_content(user_text: str, video_url: str) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": user_text},
        {"type": "video_url", "video_url": {"url": video_url}},
    ]


def _looks_like_url_fetch_error(status_code: int, body_text: str) -> bool:
    if status_code not in (400, 415, 422):
        return False
    lowered = (body_text or "").lower()
    return any(hint in lowered for hint in _URL_FETCH_ERROR_HINTS)


def _extract_reasoning_text(message: dict[str, Any]) -> str:
    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    details = message.get("reasoning_details")
    if isinstance(details, list):
        parts: list[str] = []
        for item in details:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("reasoning") or ""
            if isinstance(text, str) and text:
                parts.append(text)
        if parts:
            return "\n".join(parts).strip()
    return ""


def _partial_review_result(prose_text: str, reason: str) -> dict[str, Any]:
    _logger.warning(
        "T2AV review: parse fallback to verdict=REVIEW (%s). "
        "Raw response length=%d chars. Excerpt: %r",
        reason, len(prose_text or ""), (prose_text or "")[:500],
    )
    safe = (prose_text or "").strip()
    annotated = (
        f"[PARSE_WARNING: {reason}. Manual review required.]\n\n{safe}"
    )[:65536]
    return {
        "verdict": "REVIEW",
        "prose": annotated,
        "findings": [],
        "fatal_count": 0,
        "major_count": 0,
        "minor_count": 0,
        "unverifiable_count": 0,
        "regenerate_recommended": False,
        "rebuilder_hint": "",
        "rendered_info": "{}",
        "raw_json": "{}",
    }


def parse_review_output(text: str) -> dict[str, Any]:
    matches = list(_JSON_BLOCK_RE.finditer(text))
    if not matches:
        return _partial_review_result(
            text, "No fenced JSON block in reviewer output",
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
        return _partial_review_result(
            text,
            f"JSON block found but did not match schema. "
            f"Last decode error: {last_decode_error}",
        )
    verdict = (parsed.get("verdict") or "").strip().upper()
    if verdict not in _VALID_VERDICTS:
        return _partial_review_result(
            text,
            f"Reviewer returned invalid verdict {verdict!r}. "
            f"Expected one of {_VALID_VERDICTS}",
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
            "Configure in Settings > T2AV."
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
        prefix="t2av_review_", suffix=".mp4", delete=False
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


def _post_openrouter(
    *,
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_HTTP_REFERER,
        "X-Title": OPENROUTER_APP_TITLE,
    }
    return requests.post(
        OPENROUTER_API_URL,
        json=payload,
        headers=headers,
        timeout=timeout,
    )


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
    previous_failures: list[dict] | None = None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    request_timeout: int = DEFAULT_OPENROUTER_TIMEOUT,
) -> dict[str, Any]:
    if not openrouter_api_key:
        raise ReviewAuthError(
            "OpenRouter API key required for OpenRouter review. "
            "Configure in Settings > T2AV > OpenRouter API Key."
        )
    system_prompt = load_review_system_prompt()
    user_text = _build_review_user_text(
        enriched_prompt=enriched_prompt,
        category=category,
        style=style,
        priority=priority,
        duration_seconds=duration_seconds,
        resolution=resolution,
        previous_failures=previous_failures,
    )

    def _payload(content_blocks: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content_blocks},
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        }
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        return body

    url_content = _build_multimodal_content(user_text, video_url)
    use_base64 = False
    last_exc: BaseException | None = None
    response_json: dict[str, Any] | None = None

    for attempt in range(1, max_attempts + 1):
        if use_base64:
            try:
                video_bytes = _download_video_bytes(video_url)
            except Exception as exc:
                raise ReviewError(
                    f"Base64 fallback failed to download video from "
                    f"{video_url!r}: {exc.__class__.__name__}: {exc}"
                ) from exc
            data_url = _encode_video_data_url(video_bytes)
            content_blocks = _build_multimodal_content(user_text, data_url)
            _logger.info(
                "OpenRouter review: using base64 inline fallback "
                "(%.2f MB encoded) on attempt %d/%d",
                len(data_url) / 1e6, attempt, max_attempts,
            )
        else:
            content_blocks = url_content

        try:
            resp = _post_openrouter(
                api_key=openrouter_api_key,
                payload=_payload(content_blocks),
                timeout=request_timeout,
            )
            if resp.status_code in (401, 403):
                raise ReviewAuthError(
                    f"OpenRouter rejected credentials (HTTP {resp.status_code}): "
                    f"{resp.text[:200]}"
                )
            if not use_base64 and _looks_like_url_fetch_error(resp.status_code, resp.text):
                _logger.warning(
                    "OpenRouter review attempt %d/%d: model could not fetch "
                    "video URL (HTTP %d); retrying with base64 inline fallback. "
                    "Body: %s",
                    attempt, max_attempts, resp.status_code, resp.text[:200],
                )
                use_base64 = True
                continue
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
        except ReviewError:
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
        message = response_json["choices"][0]["message"]
        text = message.get("content") or ""
    except (KeyError, TypeError, IndexError) as e:
        raise ReviewError(
            f"Unexpected OpenRouter response shape: {response_json!r}"
        ) from e
    if not text or not text.strip():
        finish = ""
        try:
            finish = response_json["choices"][0].get("finish_reason") or ""
        except (KeyError, TypeError, IndexError):
            pass
        raise ReviewError(
            f"Reviewer returned empty text (finish_reason={finish!r}). "
            "Consider raising max_tokens if reasoning effort is high."
        )
    parsed = parse_review_output(text)
    usage = response_json.get("usage") or {}
    parsed["input_tokens"] = int(usage.get("prompt_tokens") or 0)
    parsed["output_tokens"] = int(usage.get("completion_tokens") or 0)
    parsed["request_id"] = response_json.get("id") or ""
    parsed["num_frames"] = 0
    parsed["provider"] = "openrouter"
    parsed["reasoning_text"] = _extract_reasoning_text(message) if isinstance(message, dict) else ""
    parsed["video_delivery"] = "base64" if use_base64 else "url"
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
    max_tokens: int | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float = DEFAULT_TOP_P,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    previous_failures: list[dict] | None = None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
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
            max_tokens=max_tokens if max_tokens is not None else DEFAULT_BEDROCK_MAX_TOKENS,
            temperature=temperature, top_p=top_p,
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
            max_tokens=max_tokens if max_tokens is not None else DEFAULT_OPENROUTER_MAX_TOKENS,
            temperature=temperature, top_p=top_p,
            max_attempts=max_attempts,
            previous_failures=previous_failures,
            reasoning_effort=reasoning_effort,
        )
    else:
        raise ReviewConfigError(
            f"Unknown review provider {provider_norm!r}. "
            f"Expected 'bedrock' or 'openrouter'."
        )
