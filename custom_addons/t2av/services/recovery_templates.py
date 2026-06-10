from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

_DEFAULT_DURATION = 10
_DEFAULT_TOPIC = "the scene"
_DEFAULT_SUB_CATEGORY = "general subject"
_DEFAULT_CATEGORY = "general"
_DEFAULT_STYLE = "casual"

_STYLES_WITH_LONG_BAND = frozenset({"precise", "narrative", "exhaustive", "creative"})
_STYLES_WITH_MEDIUM_BAND = frozenset({"terse"})

TEMPLATES: dict[tuple[str, str], str] = {
    ("av_sync_sound_effects", "casual"): (
        "A {duration}-second clip of {topic}. The camera holds steady on "
        "{sub_category} while the sound source is clearly visible and "
        "synchronised. Subtle ambient room tone fills the background; the "
        "primary sound effect lands precisely on the visible action."
    ),
    ("av_sync_sound_effects", "precise"): (
        "A precisely framed {duration}-second video centred on {topic}. The "
        "camera tracks slowly across the scene as {sub_category} produces a "
        "distinct, well-synchronised sound. Every audio event lines up with "
        "a clearly visible motion. The lighting is neutral and even, the "
        "background uncluttered. Microphone placement favours the subject. "
        "Sound design emphasises detail: the texture of the action, the "
        "decay tail, and the room's natural acoustics. No music, no "
        "narration, no overlapping voices. The result is a clean reference "
        "of action-to-sound correspondence suitable for evaluation."
    ),
    ("multi_speaker_dialogue", "casual"): (
        "A {duration}-second exchange between two speakers about {topic}. "
        "The camera alternates between them with a soft cut. Each line is "
        "clearly audible above gentle room tone. {sub_category} provides "
        "the natural backdrop."
    ),
    ("human_activities", "casual"): (
        "A {duration}-second handheld shot of a person performing {topic}. "
        "The camera follows the motion at a relaxed pace. {sub_category} "
        "frames the action; natural light and ambient sound complete the "
        "atmosphere."
    ),
    ("high_motion_action", "casual"): (
        "A {duration}-second high-energy clip of {topic}. The camera pans "
        "smoothly to track fast movement across the frame. {sub_category} "
        "anchors the environment. Punchy ambient sound and motion-matched "
        "audio underscore the action."
    ),
    ("educational_videos", "casual"): (
        "A {duration}-second educational shot explaining {topic}. The "
        "camera holds a clean static frame on {sub_category}. A measured "
        "ambient sound bed supports the visual without distraction."
    ),
}


def tier2_template_fallback(record) -> str:
    category = _safe_str(getattr(record, "category", None), _DEFAULT_CATEGORY)
    style = _safe_str(getattr(record, "style", None), _DEFAULT_STYLE).lower()
    topic = _safe_str(getattr(record, "topic", None), _DEFAULT_TOPIC)
    sub_category = _safe_str(
        getattr(record, "sub_category", None),
        category if category != _DEFAULT_CATEGORY else _DEFAULT_SUB_CATEGORY,
    )
    duration = _safe_int(getattr(record, "duration", None), _DEFAULT_DURATION)

    template = TEMPLATES.get((category, style))
    if template:
        return template.format(
            duration=duration, topic=topic, sub_category=sub_category,
            category=category,
        )

    template = TEMPLATES.get((category, _DEFAULT_STYLE))
    if template:
        _logger.info(
            "tier2 Layer B used for record category=%s style=%s "
            "(no exact template, falling back to category default '%s').",
            category, style, _DEFAULT_STYLE,
        )
        return template.format(
            duration=duration, topic=topic, sub_category=sub_category,
            category=category,
        )

    _logger.warning(
        "tier2 Layer C safety net used for record category=%s style=%s. "
        "Consider adding a template to recovery_templates.TEMPLATES.",
        category, style,
    )
    return _build_safety_net(
        category=category, style=style, topic=topic,
        sub_category=sub_category, duration=duration,
    )


def _build_safety_net(
    *, category: str, style: str, topic: str, sub_category: str, duration: int,
) -> str:
    base = (
        f"A {duration}-second video showcasing {topic}. The scene focuses "
        f"on {sub_category} in a {category} setting. Standard cinematography "
        f"with steady framing; ambient audio complements the action."
    )
    if style in _STYLES_WITH_LONG_BAND:
        extension = (
            f" Camera movement is deliberate and unobtrusive, holding "
            f"compositions long enough for detail to register. Lighting is "
            f"natural and balanced across the frame. The {sub_category} "
            f"subject remains visually dominant while the surrounding "
            f"environment provides context. Sound design layers a quiet "
            f"ambient bed with foreground audio cues that match on-screen "
            f"actions; no music or narration competes with the scene. The "
            f"overall pacing is unhurried, allowing the {topic} action to "
            f"breathe and resolve without rushed cuts. The visual register "
            f"matches the {style} treatment requested for this category."
        )
        return base + extension
    if style in _STYLES_WITH_MEDIUM_BAND:
        extension = (
            f" Tight framing keeps the {sub_category} centred. The audio "
            f"track stays clean and motion-matched, supporting the {topic} "
            f"without overstatement."
        )
        return base + extension
    return base


def _safe_str(value, default: str) -> str:
    if value is None:
        return default
    s = str(value).strip()
    return s or default


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
