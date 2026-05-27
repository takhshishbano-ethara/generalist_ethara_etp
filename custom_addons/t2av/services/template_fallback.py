from __future__ import annotations

import re
from typing import Dict, List, Tuple

from . import auto_repair as _ar


MANDATORY_SUFFIX_STEREO = _ar.MANDATORY_SUFFIX_STEREO


_BAND_BY_STYLE = {
    "casual": (150, 220),
    "precise": (180, 240),
    "narrative": (200, 280),
    "terse": (80, 140),
    "exhaustive": (240, 280),
    "creative": (180, 240),
}
_DEFAULT_BAND = (180, 240)
_DEFAULT_STYLE = "precise"


_BLOCK_ORDER = ("open", "action", "environment", "lighting", "camera", "style", "audio")


_CATEGORY_TEMPLATES: Dict[str, Dict[str, str]] = {
    "animals_wildlife": {
        "subject_default": "wild animal",
        "open": "A {subject} moves slowly across the open ground, its body angled toward a patch of dry grass on the far side of the frame.",
        "action": "The animal pauses, lowers its head and steps forward, sniffing the surface and listening to the passing wind with ears tilted toward the canopy.",
        "environment": "Tall reeds bend along the edge of the path and small motes of dust drift past as the body shifts weight from one side to the other, exposing a curved line of muscle along the flank.",
        "lighting": "Directional sunlight at five thousand six hundred Kelvin falls from the open sky behind the subject and rakes across the fur, casting a ground shadow that follows the body as it steps.",
        "camera": "The camera holds at locked static, framed waist-up from a slightly raised angle just above the ground level, with the subject placed at the lower third of the frame.",
        "style": "The mood is observational and patient, a single uninterrupted field recording with natural pacing.",
        "audio": "Audio: ambient wind through dry grass, leaves rustling at the path edge, occasional bird calls from the canopy beyond.",
    },
    "food": {
        "subject_default": "cook",
        "open": "A {subject} stands at a wooden cutting board, hands resting beside a pile of flour and a folded wedge of dough.",
        "action": "The cook opens both palms and presses the dough forward, then folds it twice with steady deliberate motions, sets the dough aside and reaches for a small bowl of olive oil.",
        "environment": "A bowl of chopped herbs sits to the right and a stack of white plates leans against the back wall, with a glass of water beside the board reflecting the lamp above.",
        "lighting": "Overhead light at three thousand two hundred Kelvin falls onto the counter surface from a single hanging lamp and rakes across the dough, giving the flour a clear texture and the wood grain a directional shadow.",
        "camera": "The camera holds at static close-up on the counter from a slightly elevated angle, framing the dough and both hands within the lower two thirds of the frame.",
        "style": "The mood is observational and methodical, an everyday kitchen recording with natural pacing.",
        "audio": "Audio: knife on board, simmering pan in the background, ambient kitchen room tone with a faint refrigerator hum.",
    },
    "cars": {
        "subject_default": "driver",
        "open": "A {subject} sits behind the wheel of a parked sedan in a paved lot, both hands resting on the steering wheel and the engine idling at low speed.",
        "action": "The driver turns the key fully, pushes the gear lever forward and presses the accelerator, then pulls the wheel to the right as the car rolls out of the parking bay.",
        "environment": "A row of painted parking lines marks the lot surface and a curb separates the bay from a strip of trimmed grass, while a wooden fence behind the lot frames the background.",
        "lighting": "Side light at fifty six hundred Kelvin enters from the driver window and falls across the dashboard, throwing a clear directional shadow along the centre console and the gear lever.",
        "camera": "The camera holds at single pan-right, framed medium on the driver and the upper dashboard from a slightly elevated angle on the passenger side.",
        "style": "The mood is observational and measured, a routine commute moment with natural pacing.",
        "audio": "Audio: engine idle hum, tyres rolling on paved surface, faint ventilation fan from the dashboard vents.",
    },
    "fashion": {
        "subject_default": "model",
        "open": "A {subject} stands in front of a painted concrete wall, facing slightly off-axis with one hand resting at the waist and the other holding the strap of a canvas bag.",
        "action": "The model turns the head toward the camera, lifts the bag forward and steps once to the right, then settles into a relaxed pose with shoulders squared toward the wall.",
        "environment": "A wide pavement strip runs in front of the wall and a row of planted trees lines the far edge of the frame, with a few fallen leaves scattered on the ground.",
        "lighting": "Side light at fifty four hundred Kelvin falls from the open sky to camera-left and rakes across the fabric of the jacket, giving the weave a clear texture and the wall a graded shadow.",
        "camera": "The camera holds at static medium, framed three-quarter length on the model from a slightly raised angle, with the wall filling the upper half of the frame.",
        "style": "The mood is composed and observational, a documentary portrait take with natural pacing.",
        "audio": "Audio: faint street traffic in the background, fabric rustling as the bag moves, occasional footsteps on the pavement nearby.",
    },
    "sports": {
        "subject_default": "athlete",
        "open": "A {subject} stands at the edge of a paved court, holding a ball in both hands and shifting weight forward onto the front foot.",
        "action": "The athlete dribbles the ball twice, steps forward toward the painted line and throws the ball overhead, then turns and jogs back to the starting mark with steady controlled breathing.",
        "environment": "Painted court lines run across the surface and a metal frame stands at the far end of the court, while a chain-link fence beyond the line frames the background.",
        "lighting": "Overhead light at fifty eight hundred Kelvin falls from the open sky and rakes across the surface, giving the painted lines a clear edge and the body a graded shadow on the court.",
        "camera": "The camera holds at handheld follow, framed medium on the athlete from a slightly raised angle behind the painted line.",
        "style": "The mood is observational and grounded, a single uninterrupted training take with natural pacing.",
        "audio": "Audio: ball bouncing on the court surface, shoes squeaking on paved ground, faint crowd noise from a nearby field.",
    },
    "nature_landscape": {
        "subject_default": "open landscape",
        "open": "A wide stretch of {subject} fills the frame, with a low ridge of rolling hills along the horizon and a slow-moving river curving across the middle ground.",
        "action": "A breeze passes through the tall grass on the near bank and the surface of the river shifts in slow ripples, while a single bird flies across the upper third of the frame from right to left.",
        "environment": "A line of trees lines the far bank and a few large rocks break the river surface near the middle of the frame, while loose grass and small wildflowers cover the foreground.",
        "lighting": "Late afternoon sunlight at fifty four hundred Kelvin falls from the upper right and rakes across the landscape, giving the river surface a graded reflection and the trees a clear directional shadow.",
        "camera": "The camera holds at locked static, framed wide on the landscape from a slightly raised angle, with the horizon placed near the upper third of the frame.",
        "style": "The mood is observational and contemplative, a single uninterrupted landscape take with natural pacing.",
        "audio": "Audio: ambient wind through the grass, water lapping at the river bank, occasional bird calls from the trees beyond.",
    },
    "urban_street": {
        "subject_default": "pedestrian",
        "open": "A {subject} walks along a paved sidewalk in a city block, carrying a folded jacket over one arm and a phone in the other hand.",
        "action": "The pedestrian steps off the curb at the crossing, walks across the painted lines and reaches the opposite sidewalk, then turns the head once toward the camera before continuing along the block.",
        "environment": "A row of parked cars lines the kerb on the near side and a line of shopfronts runs along the opposite sidewalk, while a few scattered pedestrians move in the background.",
        "lighting": "Overcast daylight at sixty two hundred Kelvin falls evenly across the street and gives the painted lines a clear edge, while the shopfront windows reflect the open sky.",
        "camera": "The camera holds at handheld follow, framed medium on the pedestrian from a slightly elevated angle on the opposite sidewalk.",
        "style": "The mood is observational and unposed, a documentary street take with natural pacing.",
        "audio": "Audio: faint traffic hum in the background, footsteps on paved ground, occasional voices from the opposite sidewalk.",
    },
    "indoor_lifestyle": {
        "subject_default": "person",
        "open": "A {subject} sits on a fabric sofa in a small apartment, holding an open book in both hands and resting the feet on a low wooden table.",
        "action": "The person turns the page, leans forward and sets the book down on the table, then reaches for a ceramic mug and lifts it toward the mouth with both hands.",
        "environment": "A potted plant sits on the windowsill behind the sofa and a folded blanket lies across the armrest, while a stack of books and a single lamp occupy the table beside the mug.",
        "lighting": "Window light at fifty two hundred Kelvin enters from camera-left and falls across the sofa fabric, giving the page of the book a clear contrast and the wall a graded shadow.",
        "camera": "The camera holds at static medium, framed waist-up on the person from a slightly elevated angle on the opposite side of the table.",
        "style": "The mood is observational and unhurried, a quiet apartment recording with natural pacing.",
        "audio": "Audio: ambient room tone, pages turning under the hand, faint traffic from outside the window.",
    },
    "multi_speaker_dialogue": {
        "subject_default": "two coworkers",
        "open": "Two coworkers stand at a kitchen counter inside a small office, leaning lightly on the edge with paper cups of coffee in their hands.",
        "action": "The first coworker turns and says, \"I think we should ship the update on Monday morning.\" The second nods and answers, \"That gives us enough time to test the new feature.\" They both step closer to a laptop on the counter and the first opens the lid to show a list of items.",
        "environment": "A row of small windows lines the wall behind them and a coffee machine sits on the counter to the right, while a corkboard covered in printed notes hangs above the counter on the left.",
        "lighting": "Overhead light at four thousand two hundred Kelvin falls evenly across the counter and gives the laptop screen a clear contrast, while the windows behind them admit a graded directional fill.",
        "camera": "The camera holds at locked static, framed waist-up on both people from a slight side angle, with the laptop visible in the lower frame.",
        "style": "The mood is conversational and grounded, a routine workday moment with natural pacing.",
        "audio": "Audio: office room tone, faint ventilation fan, occasional keystrokes on the laptop, two voices speaking at moderate volume.",
    },
}


_DEFAULT_TEMPLATE: Dict[str, str] = {
    "subject_default": "person",
    "open": "A {subject} stands in an open indoor space, facing slightly off-axis with both hands relaxed at the sides and the weight balanced evenly on both feet.",
    "action": "The person steps forward, turns the head once to the right and lifts both hands toward the centre of the frame, then settles back into a relaxed standing pose with shoulders squared.",
    "environment": "A plain painted wall fills the background and a wooden floor extends across the lower third of the frame, while a single piece of furniture sits at the far edge of the room.",
    "lighting": "Overhead light at fifty four hundred Kelvin falls evenly across the space and gives the wall a graded shadow, while a side window admits a directional fill from camera-left.",
    "camera": "The camera holds at static medium, framed waist-up on the subject from a slightly elevated angle.",
    "style": "The mood is observational and unposed, a single uninterrupted indoor take with natural pacing.",
    "audio": "Audio: ambient room tone, faint footsteps on the wooden floor, occasional fabric rustling from the clothing.",
}


_FORBIDDEN_SUBJECT_PATTERNS = re.compile(
    r"\b(drone|aerial|crane shot|anamorphic|imax|prores|steadicam|gimbal rig|"
    r"disney|pixar|marvel|olympic|nike|adidas|samsung|sony|"
    r"stunning|breathtaking|epic|majestic|ethereal|magical|"
    r"cuts? to|fades? to|crossfade|montage|split screen|"
    r"4\s?K|8\s?K|UHD|Ultra\s?HD)\b",
    re.IGNORECASE,
)


def _sanitize_subject(raw: str, default: str) -> str:
    if not raw:
        return default
    cleaned = _FORBIDDEN_SUBJECT_PATTERNS.sub("", raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r'["\'\u201c\u201d\u2018\u2019]', "", cleaned)
    cleaned = re.sub(r"[^\w\s\-]", "", cleaned)
    words = cleaned.split()
    if not words:
        return default
    return " ".join(words[:6])


def _resolve_subject(metadata: dict, template: Dict[str, str]) -> str:
    default = template.get("subject_default") or "subject"
    topic = (metadata.get("Topic") or "").strip()
    if topic:
        sanitized = _sanitize_subject(topic, default)
        if sanitized and sanitized != default:
            return sanitized
    prompt = (metadata.get("Prompt") or "").strip()
    if prompt:
        return _sanitize_subject(prompt, default)
    return default


def _render_blocks(template: Dict[str, str], subject: str) -> List[Tuple[str, str]]:
    rendered: List[Tuple[str, str]] = []
    for key in _BLOCK_ORDER:
        block = template.get(key) or ""
        if not block:
            continue
        rendered.append((key, block.replace("{subject}", subject).strip()))
    return rendered


def _word_count(text: str) -> int:
    return len(text.split())


def _band_for_style(style: str) -> Tuple[int, int]:
    style_norm = (style or "").strip().lower()
    return _BAND_BY_STYLE.get(style_norm, _DEFAULT_BAND)


_CRITICAL_KEYS = ("action", "camera", "audio")
_OPTIONAL_DROP_ORDER = ("style", "lighting", "environment")


_PAD_SENTENCE = (
    "The take continues in a single steady recording across the full duration "
    "with no edits or staging beyond what is already in the frame."
)
_TERSE_PAD_SENTENCE = (
    "The take continues steadily across the full duration."
)


def _assemble_within_band(
    blocks: List[Tuple[str, str]], lo: int, hi: int, pad_sentence: str
) -> str:
    by_key: Dict[str, str] = dict(blocks)
    ordered_keys = [k for k, _ in blocks]
    suffix_wc = len(MANDATORY_SUFFIX_STEREO.split())

    def body_wc() -> int:
        return sum(_word_count(by_key[k]) for k in ordered_keys if k in by_key)

    for drop in _OPTIONAL_DROP_ORDER:
        if body_wc() + suffix_wc <= hi:
            break
        if drop in ordered_keys and drop not in _CRITICAL_KEYS:
            ordered_keys.remove(drop)

    if body_wc() + suffix_wc > hi and "open" in ordered_keys:
        wc_other = sum(_word_count(by_key[k]) for k in ordered_keys if k != "open")
        open_budget = hi - suffix_wc - wc_other
        open_words = by_key["open"].split()
        if open_budget >= 6:
            trimmed = " ".join(open_words[:open_budget]).rstrip(",. ") + "."
            by_key["open"] = trimmed
        else:
            ordered_keys.remove("open")

    body = " ".join(by_key[k] for k in ordered_keys if k in by_key)

    while _word_count(body) + suffix_wc < lo:
        body = (body + " " + pad_sentence).strip()

    return (body + " " + MANDATORY_SUFFIX_STEREO).strip()


def build(metadata: dict, style: str = _DEFAULT_STYLE) -> str:
    category = (metadata.get("Category") or "").strip().lower()
    template = _CATEGORY_TEMPLATES.get(category, _DEFAULT_TEMPLATE)
    subject = _resolve_subject(metadata or {}, template)
    blocks = _render_blocks(template, subject)
    lo, hi = _band_for_style(style)
    style_norm = (style or "").strip().lower()
    pad = _TERSE_PAD_SENTENCE if style_norm == "terse" else _PAD_SENTENCE
    assembled = _assemble_within_band(blocks, lo, hi, pad)
    scrubbed, _ = _ar.repair_all(assembled, category=category, style=style_norm)
    return scrubbed
