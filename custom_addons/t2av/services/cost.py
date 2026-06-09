"""Seedance 2.0 token-cost formula (pure Python, no Odoo imports).

Encodes the OpenRouter/ByteDance Seedance 2.0 pricing model:

    tokens = (width * height * fps * duration) / 1024
    usd    = tokens * (USD_PER_MTOK / 1_000_000)

with ``fps = 24`` and ``USD_PER_MTOK = 7.0`` (i.e. $7 per 1M tokens).

Resolution + aspect-ratio handling
----------------------------------
For a 16:9 aspect ratio the table ``RESOLUTION_PIXELS_16_9`` gives the
canonical landscape pixel dimensions. For other aspect ratios we keep the
*short side* (i.e. the resolution height of the landscape baseline) fixed
and compute the long side from the ratio. Portrait ratios swap width and
height; 1:1 uses the baseline height for both. All returned dimensions are
rounded to the nearest even integer so they remain encoder-friendly.
"""

from __future__ import annotations

__all__ = [
    "estimate",
    "pixels_for",
    "USD_PER_MTOK",
    "FPS",
    "RESOLUTION_PIXELS_16_9",
]


USD_PER_MTOK = 7.0
FPS = 24

# 16:9 baseline (width, height). For other aspect ratios we swap or letterbox-equivalent;
# v1 keeps it simple by swapping width/height for portrait and computing the rectangle for
# square / 21:9 by holding the long side at the resolution height.
RESOLUTION_PIXELS_16_9 = {
    "480p":  (854, 480),
    "720p":  (1280, 720),
    "1080p": (1920, 1080),
}


def _round_even(value: float) -> int:
    """Round to the nearest non-negative even integer (floor-based)."""
    return int(value // 2 * 2)


def _parse_ratio(aspect_ratio: str) -> tuple[int, int]:
    """Parse a string like ``"16:9"`` into ``(16, 9)``.

    Raises ``ValueError`` if the input is malformed or contains
    non-positive components.
    """
    if not isinstance(aspect_ratio, str):
        raise ValueError(f"Malformed aspect_ratio: {aspect_ratio!r}")
    parts = aspect_ratio.split(":")
    if len(parts) != 2:
        raise ValueError(f"Malformed aspect_ratio: {aspect_ratio!r}")
    try:
        num = int(parts[0])
        den = int(parts[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Malformed aspect_ratio: {aspect_ratio!r}") from exc
    if num <= 0 or den <= 0:
        raise ValueError(f"Malformed aspect_ratio: {aspect_ratio!r}")
    return num, den


def pixels_for(resolution: str, aspect_ratio: str) -> tuple[int, int]:
    """Return ``(width, height)`` for the resolution.

    OpenRouter bills Seedance 2.0 at flat per-second rates per resolution
    ($0.06726/480p, $0.15120/720p, $0.34020/1080p), equivalent to using
    the 16:9 baseline pixel count regardless of ``aspect_ratio``. The
    parameter is validated for format only. Switching to a provider with
    aspect-specific pricing (fal.ai, ByteDance direct) would require
    restoring per-aspect dimensions.
    """
    if resolution not in RESOLUTION_PIXELS_16_9:
        raise ValueError(f"Unknown resolution: {resolution}")
    _parse_ratio(aspect_ratio)
    return RESOLUTION_PIXELS_16_9[resolution]


def estimate(resolution: str, duration: int, aspect_ratio: str) -> tuple[int, float]:
    """Return ``(tokens, usd)`` using the Seedance 2.0 formula."""
    width, height = pixels_for(resolution, aspect_ratio)
    tokens = (width * height * FPS * int(duration)) // 1024
    usd = tokens * USD_PER_MTOK / 1_000_000
    return tokens, usd
