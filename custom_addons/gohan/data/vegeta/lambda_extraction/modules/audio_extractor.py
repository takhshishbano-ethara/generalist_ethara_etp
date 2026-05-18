"""
Audio Extractor - captures audio files, Web Audio API usage, and audio-to-scroll mappings.

Extracts:
  - Intercepted audio file bytes (response interception during page load)
  - Web Audio API usage (AudioContext, BufferSource, decodeAudioData)
  - Library detection (Howler.js, Tone.js, native Web Audio)
  - Play events mapped to scroll positions
  - DOM <audio> elements and performance resource entries
  - Three.js positional/spatial audio

Main entry points:
    setup_audio_intercept(page) -> dict  (call BEFORE navigation)
    async def extract_audio(page, output_dir: str) -> dict
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths to injectable JS
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_AUDIO_JS = _SCRIPTS_DIR / "inject_audio_extractor.js"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AUDIO_MIME_TYPES: set[str] = {
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/mp4",
    "audio/webm", "audio/x-m4a", "audio/aac", "audio/flac",
    "audio/x-wav",
}

AUDIO_EXTENSIONS: set[str] = {".mp3", ".ogg", ".wav", ".m4a", ".webm", ".aac", ".flac"}


# ---------------------------------------------------------------------------
# Response interception (must be wired BEFORE page.goto)
# ---------------------------------------------------------------------------
def setup_audio_intercept(page: Page) -> dict[str, bytes]:
    """Register response listener to capture audio file bytes during page load.

    Must be called BEFORE page.goto(). Returns a dict that will be
    populated as audio responses arrive.
    """
    intercepted: dict[str, bytes] = {}
    _total_bytes = [0]  # mutable container for closure
    _MAX_AUDIO_BYTES = 50 * 1024 * 1024  # 50MB cap to prevent OOM

    async def _on_response(response) -> None:
        try:
            if _total_bytes[0] >= _MAX_AUDIO_BYTES:
                return  # Stop collecting once cap reached
            url: str = response.url
            ct = (response.headers.get("content-type") or "").lower().split(";")[0].strip()
            parsed = urlparse(url)
            ext = os.path.splitext(parsed.path.split("?")[0])[1].lower()

            is_audio = ct in AUDIO_MIME_TYPES or ext in AUDIO_EXTENSIONS
            if not is_audio:
                return
            if not response.ok:
                return

            body = await response.body()
            if body and len(body) > 100:
                if _total_bytes[0] + len(body) > _MAX_AUDIO_BYTES:
                    logger.debug("Audio intercept cap reached (%d bytes), skipping %s",
                                 _total_bytes[0], url)
                    return
                intercepted[url] = body
                _total_bytes[0] += len(body)
                logger.debug("Audio intercepted: %s (%d bytes, total: %d)",
                             url, len(body), _total_bytes[0])
        except Exception as exc:
            logger.debug("Audio intercept error: %s", exc)

    page.on("response", _on_response)
    return intercepted


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------
async def extract_audio(page: Page, output_dir: str, intercepted_audio: dict[str, bytes] | None = None) -> dict:
    """Extract audio data from the page and save intercepted audio files.

    Args:
        page: Playwright Page (after navigation and page load).
        output_dir: Root output directory for this site run.
        intercepted_audio: Dict returned by setup_audio_intercept(). If None,
            only JS-detected audio URLs will be attempted for download.

    Returns:
        Dict with audio library info, file list, play events, and saved files.
    """
    if intercepted_audio is None:
        intercepted_audio = {}

    audio_dir = os.path.join(output_dir, "assets", "audio")
    os.makedirs(audio_dir, exist_ok=True)

    # Read captured data from early hook (injected via addInitScript)
    try:
        audio_data: dict = await page.evaluate("() => window.__audioCapture || {}")
    except Exception as exc:
        logger.warning("Audio capture read failed: %s", exc)
        audio_data = {}

    # Save intercepted audio files to disk
    saved_files: list[dict] = []
    for url, body in intercepted_audio.items():
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path.split("?")[0]) or "audio_unknown"
        if not any(filename.endswith(ext) for ext in AUDIO_EXTENSIONS):
            filename += ".mp3"

        filepath = os.path.join(audio_dir, filename)
        base, ext_str = os.path.splitext(filepath)
        counter = 1
        while os.path.exists(filepath):
            filepath = f"{base}_{counter}{ext_str}"
            counter += 1

        try:
            with open(filepath, "wb") as f:
                f.write(body)
            saved_files.append({
                "url": url,
                "filename": os.path.basename(filepath),
                "size_bytes": len(body),
                "path": os.path.relpath(filepath, output_dir),
            })
            logger.info("Saved audio: %s (%d bytes)", os.path.basename(filepath), len(body))
        except Exception as exc:
            logger.warning("Failed to save audio %s: %s", url, exc)

    # Also try downloading audio URLs found by the JS extractor but not intercepted
    audio_files_from_js: list[dict] = audio_data.get("audioFiles", [])
    for entry in audio_files_from_js:
        url = entry.get("url", "")
        if not url or url in intercepted_audio:
            continue
        try:
            resp = await page.request.get(url)
            if resp.ok:
                body = await resp.body()
                if body and len(body) > 100:
                    parsed = urlparse(url)
                    filename = os.path.basename(parsed.path.split("?")[0]) or "audio_unknown"
                    filepath = os.path.join(audio_dir, filename)
                    base, ext_str = os.path.splitext(filepath)
                    counter = 1
                    while os.path.exists(filepath):
                        filepath = f"{base}_{counter}{ext_str}"
                        counter += 1
                    with open(filepath, "wb") as f:
                        f.write(body)
                    saved_files.append({
                        "url": url,
                        "filename": os.path.basename(filepath),
                        "size_bytes": len(body),
                        "path": os.path.relpath(filepath, output_dir),
                    })
        except Exception as exc:
            logger.debug("Audio download failed for %s: %s", url, exc)

    result: dict = {
        "audioLibrary": audio_data.get("audioLibrary"),
        "webAudioUsage": audio_data.get("webAudioUsage", False),
        "spatialAudio": audio_data.get("spatialAudio", False),
        "audioFiles": audio_data.get("audioFiles", []),
        "audioElements": audio_data.get("audioElements", []),
        "bufferSources": audio_data.get("bufferSources", []),
        "howlerSounds": audio_data.get("howlerSounds", []),
        "audioContexts": audio_data.get("audioContexts", []),
        "playEvents": audio_data.get("playEvents", []),
        "savedFiles": saved_files,
        "totalIntercepted": len(intercepted_audio),
    }

    # Summary logging
    lib = result["audioLibrary"] or "none"
    n_files = len(saved_files)
    n_play = len(result["playEvents"])
    logger.info(
        "Audio extraction complete: library=%s, saved=%d files, playEvents=%d",
        lib, n_files, n_play,
    )

    return result
