"""
Phase gate — deterministic preset routing based on site_discovery results.

Accepts the site_discovery dict (already produced by Phase 1) and returns
phase-skip decisions + tuning overrides for the remaining pipeline phases.

No LLM, no network calls, no external dependencies. Returns in <10ms.

Phase names (matching main.py orchestration order):
    site_discovery, network_interception, style_extraction, animation_extraction,
    asset_collection, responsive_analysis, wireframe_generation, codegen_export,
    prd_generation, scoring_qc

Usage:
    from modules.phase_gate import get_phase_config

    config = get_phase_config(site_discovery_result)
    skip = config["skip_phases"]       # list[str] — phases to skip entirely
    tuning = config["tuning"]          # dict — overrides (timeouts, limits, etc.)
    preset = config["preset"]          # str — which preset was matched
    reasoning = config["reasoning"]    # str — why this preset was chosen
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase names as used by main.py
# ---------------------------------------------------------------------------
PHASE_NAMES = [
    "site_discovery",
    "network_interception",
    "style_extraction",
    "animation_extraction",
    "asset_collection",
    "responsive_analysis",
    "wireframe_generation",
    "codegen_export",
    "prd_generation",
    "scoring_qc",
]

# ---------------------------------------------------------------------------
# Preset definitions — mapped to the 5 Leviathon categories from config.py
# ---------------------------------------------------------------------------
PRESETS: dict[str, dict[str, Any]] = {
    "normal_website": {
        "description": (
            "Typography-driven site, no heavy animation. "
            "Reduce animation scroll steps, skip wireframe deep-scan."
        ),
        "skip_phases": [],
        "tuning": {
            "animation_scroll_steps": 5,
            "animation_timeout_ms": 15000,
            "wireframe_deep_scan": False,
            "hover_settle_ms": 300,
            "max_runtime_seconds": 300,
            "asset_max_images": 30,
            "responsive_extra_breakpoints": False,
        },
    },
    "cool_transition": {
        "description": (
            "GSAP + ScrollTrigger choreography, page transitions. "
            "Full animation capture with extended timeouts."
        ),
        "skip_phases": [],
        "tuning": {
            "animation_scroll_steps": 20,
            "animation_timeout_ms": 45000,
            "wireframe_deep_scan": True,
            "hover_settle_ms": 600,
            "max_runtime_seconds": 600,
            "interaction_capture_enabled": True,
            "page_transition_capture": True,
            "scroll_trigger_patience_ms": 5000,
            "asset_max_images": 50,
            "responsive_extra_breakpoints": False,
        },
    },
    "representation_format": {
        "description": (
            "Horizontal scroll, scrollytelling, parallax layers. "
            "Full animation + responsive with extended scroll capture."
        ),
        "skip_phases": [],
        "tuning": {
            "animation_scroll_steps": 25,
            "animation_timeout_ms": 45000,
            "wireframe_deep_scan": True,
            "hover_settle_ms": 500,
            "max_runtime_seconds": 600,
            "interaction_capture_enabled": True,
            "scroll_trigger_patience_ms": 8000,
            "horizontal_scroll_detection": True,
            "parallax_layer_capture": True,
            "asset_max_images": 50,
            "responsive_extra_breakpoints": True,
            "responsive_scroll_positions": 30,
        },
    },
    "svg_vector": {
        "description": (
            "Lottie, SVG path morphing, animated vectors. "
            "Full asset collection, skip deep WebGL analysis."
        ),
        "skip_phases": [],
        "tuning": {
            "animation_scroll_steps": 15,
            "animation_timeout_ms": 30000,
            "wireframe_deep_scan": False,
            "hover_settle_ms": 500,
            "max_runtime_seconds": 450,
            "asset_max_images": 60,
            "asset_capture_svg_inline": True,
            "asset_capture_lottie_json": True,
            "webgl_deep_analysis": False,
            "responsive_extra_breakpoints": False,
        },
    },
    "3d_webgl": {
        "description": (
            "Three.js scenes, custom shaders, physics. "
            "Extended timeouts, canvas-mode screenshots, skip grid analysis. "
            "Animation extraction skipped — the DOM/GSAP/CDP scanner crashes "
            "heavy WebGL renderers; 3D motion is captured by extract_webgl."
        ),
        "skip_phases": ["animation_extraction"],
        "tuning": {
            "animation_scroll_steps": 10,
            "animation_timeout_ms": 60000,
            "wireframe_deep_scan": False,
            "wireframe_canvas_mode": True,
            "hover_settle_ms": 800,
            "max_runtime_seconds": 900,
            "webgl_deep_analysis": True,
            "webgl_sample_frames": 21,
            "webgl_sample_wait_ms": 600,
            "canvas_screenshot_enabled": True,
            "grid_analysis_skip": True,
            "asset_max_images": 40,
            "responsive_extra_breakpoints": False,
        },
    },
}

# Fallback preset when category is unrecognized
DEFAULT_PRESET = "normal_website"

# ---------------------------------------------------------------------------
# Category-to-preset mapping (config.py category keys -> preset names)
# ---------------------------------------------------------------------------
_CATEGORY_TO_PRESET: dict[str, str] = {
    # config.CATEGORIES keys
    "normal_website": "normal_website",
    "cool_transition": "cool_transition",
    "representation": "representation_format",
    "svg_vector": "svg_vector",
    "3d_webgl": "3d_webgl",
    # config.CATEGORIES display values (title-case strings from site_discovery)
    "Normal Website": "normal_website",
    "Cool Transition": "cool_transition",
    "Representation Format": "representation_format",
    "SVG & Vector Graphics": "svg_vector",
    "3D & WebGL / Game": "3d_webgl",
}


def _resolve_preset(site_discovery: dict[str, Any]) -> tuple[str, str]:
    """Determine preset from site_discovery dict.

    Returns (preset_name, reasoning).
    """
    category = site_discovery.get("category", "")
    tech_stack = site_discovery.get("tech_stack", {}) or {}

    # Direct category mapping
    if category in _CATEGORY_TO_PRESET:
        preset = _CATEGORY_TO_PRESET[category]
        return preset, f"category='{category}' -> preset='{preset}'"

    # Heuristic fallback: inspect tech_stack signals
    tech_keys = set(k.lower() for k in tech_stack.keys())

    # 3D detection
    if any(t in tech_keys for t in ("three_js", "three.js", "babylon", "pixi")):
        return "3d_webgl", "tech_stack contains 3D library (category unset)"

    # Cool Transition detection
    has_gsap = "gsap" in tech_keys
    has_scroll = any(t in tech_keys for t in ("lenis", "scrolltrigger", "scrollsmoother"))
    has_page_transition = any(t in tech_keys for t in ("barba_js", "barba", "swup", "highway", "taxi"))
    if has_gsap and (has_scroll or has_page_transition):
        return "cool_transition", "tech_stack has GSAP + scroll/transition lib (category unset)"

    # SVG/Vector detection
    if any(t in tech_keys for t in ("lottie", "rive", "anime", "snap_svg")):
        return "svg_vector", "tech_stack contains vector animation lib (category unset)"

    # Representation detection (horizontal scroll patterns)
    if has_gsap and has_scroll:
        return "representation_format", "tech_stack has GSAP + scroll hijacking (category unset)"

    return DEFAULT_PRESET, f"no category match, defaulting to '{DEFAULT_PRESET}'"


def get_phase_config(site_discovery: dict[str, Any]) -> dict[str, Any]:
    """Main entry point: determine phase gating from site_discovery results.

    Args:
        site_discovery: The dict returned by Phase 1 (site_discoverer.py).
            Expected keys: category, tech_stack, url, title, etc.

    Returns:
        {
            "preset": str,           # matched preset name
            "reasoning": str,        # why this preset was chosen
            "skip_phases": list,     # phase names to skip (empty = run all)
            "tuning": dict,          # per-phase overrides
            "description": str,      # human-readable preset description
        }
    """
    preset_name, reasoning = _resolve_preset(site_discovery)
    preset_cfg = PRESETS.get(preset_name, PRESETS[DEFAULT_PRESET])

    result = {
        "preset": preset_name,
        "reasoning": reasoning,
        "skip_phases": list(preset_cfg["skip_phases"]),
        "tuning": dict(preset_cfg["tuning"]),
        "description": preset_cfg["description"],
    }

    logger.info(f"Phase gate: preset={preset_name} — {reasoning}")
    return result


def should_run_phase(config: dict[str, Any], phase: str) -> bool:
    """Convenience: check if a phase should execute given a phase_config result.

    Args:
        config: The dict returned by get_phase_config().
        phase: One of PHASE_NAMES.

    Returns:
        True if the phase should run, False if it should be skipped.
    """
    return phase not in config.get("skip_phases", [])


def get_tuning_value(config: dict[str, Any], key: str, default: Any = None) -> Any:
    """Fetch a specific tuning override from a phase_config result.

    Args:
        config: The dict returned by get_phase_config().
        key: Tuning key name (e.g., 'animation_timeout_ms').
        default: Fallback if key not present.

    Returns:
        The tuning value, or default.
    """
    return config.get("tuning", {}).get(key, default)
