from __future__ import annotations

import re
from typing import List, Tuple

MANDATORY_SUFFIX_STEREO = (
    "1920x1080 at 30 fps, clean handheld framing, natural colour, "
    "in-camera audio at 48 kHz stereo."
)

_AI_TELL_REPLACEMENTS = {
    "—": ("-", "em-dash"),
    "–": ("-", "en-dash"),
    "×": ("x", "multiplication"),
    "…": ("...", "ellipsis-char"),
    "‘": ("'", "left-single-quote"),
    "’": ("'", "right-single-quote"),
    "“": ('"', "left-double-quote"),
    "”": ('"', "right-double-quote"),
}

_HARDWARE_REWRITES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bdrones?\b", re.IGNORECASE),
     "low handheld camera at ground level"),
    (re.compile(r"\baerial\s+(shot|view|footage|perspective)\b", re.IGNORECASE),
     "ground-level view"),
    (re.compile(r"\baerial\b", re.IGNORECASE),
     "ground-level"),
    (re.compile(r"\bcrane\s+shot\b", re.IGNORECASE),
     "static medium shot"),
    (re.compile(r"\banamorphic\b", re.IGNORECASE),
     "standard"),
    (re.compile(r"\bimax\b", re.IGNORECASE),
     "standard"),
    (re.compile(r"\b(?:90|70)\s?mm\b", re.IGNORECASE),
     "50mm"),
    (re.compile(r"\bRED\s+(?:camera|komodo|dragon|helium)\b", re.IGNORECASE),
     "professional camera"),
    (re.compile(r"\barri(?:\s+alexa)?\b", re.IGNORECASE),
     "professional camera"),
    (re.compile(r"\bprores\b", re.IGNORECASE),
     "standard codec"),
    (re.compile(r"\bdolly\s+track\b", re.IGNORECASE),
     "static track"),
    (re.compile(r"\bsteadi[\s-]?cam\b", re.IGNORECASE),
     "handheld"),
    (re.compile(r"\bsteady[\s-]?cam\b", re.IGNORECASE),
     "handheld"),
    (re.compile(r"\bgimbal\s+rig\b", re.IGNORECASE),
     "handheld"),
]

_MULTI_SHOT_REWRITES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bcut\s+back\s+to\b", re.IGNORECASE), "returning to"),
    (re.compile(r"\bmatch\s+cut\b", re.IGNORECASE),     "continuing"),
    (re.compile(r"\bjump\s+cut\b", re.IGNORECASE),      "continuing"),
    (re.compile(r"\bsmash\s+cut\b", re.IGNORECASE),     "continuing"),
    (re.compile(r"\bcuts?\s+to\b", re.IGNORECASE),      "and then"),
    (re.compile(r"\bfades?\s+to\b", re.IGNORECASE),     "blending into"),
    (re.compile(r"\bcrossfade\b", re.IGNORECASE),       "continuing"),
    (re.compile(r"\bdissolves?\s+to\b", re.IGNORECASE), "blending into"),
    (re.compile(r"\btransitions?(?:\s+to)?\b", re.IGNORECASE), "moving to"),
    (re.compile(r"\btransitioning(?:\s+to)?\b", re.IGNORECASE), "moving to"),
    (re.compile(r"\bmontage\b", re.IGNORECASE),         "continuous sequence"),
    (re.compile(r"\bthen\s+we\s+see\b", re.IGNORECASE), "and"),
    (re.compile(r"\bthen\s+it\s+shows\b", re.IGNORECASE), "and"),
    (re.compile(r"\bnext\s+shot\b", re.IGNORECASE),     "continuing"),
    (re.compile(r"\bsplit\s+screen\b", re.IGNORECASE),  "single frame"),
]

_BRAND_REWRITES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bolympics?\b", re.IGNORECASE),       "the games"),
    (re.compile(r"\btimes\s+square\b", re.IGNORECASE),  "a city square"),
    (re.compile(r"\btiktok\b", re.IGNORECASE),          "social media"),
    (re.compile(r"\bapple\s+inc\b", re.IGNORECASE),     "a tech company"),
    (re.compile(r"\bbroadway\b", re.IGNORECASE),        "a theatre district"),
    (re.compile(r"\bpixar\b", re.IGNORECASE),           "an animation studio"),
    (re.compile(r"\binstagram\b", re.IGNORECASE),       "social media"),
    (re.compile(r"\byoutube\b", re.IGNORECASE),         "a video platform"),
    (re.compile(r"\bstar\s+wars\b", re.IGNORECASE),     "a sci-fi setting"),
    (re.compile(r"\bmarvel\b", re.IGNORECASE),          "a comic universe"),
    (re.compile(r"\bdisney\b", re.IGNORECASE),          "a family studio"),
    (re.compile(r"\bspacex\b", re.IGNORECASE),          "a space company"),
    (re.compile(r"\blego\b", re.IGNORECASE),            "building blocks"),
    (re.compile(r"\btesla\b", re.IGNORECASE),           "an electric vehicle"),
    (re.compile(r"\beiffel\s+tower\b", re.IGNORECASE),  "a landmark tower"),
    (re.compile(r"\bfacebook\b", re.IGNORECASE),        "social media"),
    (re.compile(r"\b(nba|mlb|nfl|fifa)\b", re.IGNORECASE), "a sports league"),
    (re.compile(r"\bsamsung\b", re.IGNORECASE),         "an electronics brand"),
    (re.compile(r"\bnike\b", re.IGNORECASE),            "an apparel brand"),
    (re.compile(r"\bmickey\s+mouse\b", re.IGNORECASE),  "a cartoon mouse"),
    (re.compile(r"\bcoca[\s-]?cola\b", re.IGNORECASE),  "a soft drink"),
    (re.compile(r"\bmcdonald'?s?\b", re.IGNORECASE),    "a fast-food restaurant"),
    (re.compile(r"\bstarbucks\b", re.IGNORECASE),       "a coffee shop"),
    (re.compile(r"\bkodachrome\b", re.IGNORECASE),      "warm film"),
    (re.compile(r"\bhollywood\s+sign\b", re.IGNORECASE), "a hillside sign"),
    (re.compile(r"\bsnoopy\b", re.IGNORECASE),          "a cartoon dog"),
    (re.compile(r"\bmacy'?s?\b", re.IGNORECASE),        "a department store"),
    (re.compile(r"\badidas\b", re.IGNORECASE),          "an apparel brand"),
    (re.compile(r"\bsony\b", re.IGNORECASE),            "an electronics brand"),
    (re.compile(r"\bsnapchat\b", re.IGNORECASE),        "social media"),
]

_MARKETING_WORDS = {
    "stunning", "breathtaking", "epic", "mesmerising", "mesmerizing",
    "captivating", "unforgettable", "immersive", "majestic", "ethereal",
    "otherworldly", "magical", "awe-inspiring", "spellbinding",
    "enchanting", "transcendent",
}
_MARKETING_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _MARKETING_WORDS) + r")\b\s*",
    re.IGNORECASE,
)

_TOKENIZER_LEAK_PATTERNS = [
    re.compile(r"<\|start\|>", re.IGNORECASE),
    re.compile(r"<\|end\|>", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
    re.compile(r"\bassistantassistant\b", re.IGNORECASE),
    re.compile(r"\bto=selfself\b", re.IGNORECASE),
    re.compile(r"\bto=self\b", re.IGNORECASE),
    re.compile(r"\[SUB-TYPE\]", re.IGNORECASE),
    re.compile(r"\[TOPIC\]", re.IGNORECASE),
    re.compile(r"\[CATEGORY\]", re.IGNORECASE),
    re.compile(r"\bSUB-TYPE\b"),
]
_PIPELINE_LEAK_PATTERNS = [
    re.compile(r"\bannotator\b", re.IGNORECASE),
    re.compile(r"\bvendor\b", re.IGNORECASE),
    re.compile(r"\bdataset\b", re.IGNORECASE),
    re.compile(r"\btraining sample\b", re.IGNORECASE),
    re.compile(r"\bcategory\s*:", re.IGNORECASE),
    re.compile(r"\bsub[_\- ]?category\s*:", re.IGNORECASE),
    re.compile(r"\bmeta[_\- ]?prompt\b", re.IGNORECASE),
    re.compile(r"\bGenerate dataset\b", re.IGNORECASE),
    re.compile(r"\b1080p training\b", re.IGNORECASE),
    re.compile(r"\bcollection sample\b", re.IGNORECASE),
]

_DECIMAL_TS_RE = re.compile(r"\bt\s*=\s*\d+(?:\.\d+)?\s*s?\b", re.IGNORECASE)

_FORBIDDEN_RES_PATTERNS = [
    re.compile(r"\b4\s?K\b"),
    re.compile(r"\b8\s?K\b"),
    re.compile(r"\bUHD\b"),
    re.compile(r"\bUltra\s?HD\b"),
    re.compile(r"\b3840\s?[x\u00d7]\s?2160\b"),
    re.compile(r"\b7680\s?[x\u00d7]\s?4320\b"),
    re.compile(r"\bcinematic\s+texture\b", re.IGNORECASE),
    re.compile(r"\bsoft\s+film\s+grain\b", re.IGNORECASE),
    re.compile(r"\bgrain\s+overlay\b", re.IGNORECASE),
]

_SELF_REF_RE = re.compile(r"\b(row|item)\s*#?\s*\d+\b", re.IGNORECASE)

_NEGATIVE_PHRASE_PATTERNS = [
    re.compile(r"\bno cuts\b", re.IGNORECASE),
    re.compile(r"\bno time-?lapse\b", re.IGNORECASE),
    re.compile(r"\bno music\b", re.IGNORECASE),
    re.compile(r"\bwithout any cuts\b", re.IGNORECASE),
    re.compile(r"\bdon'?t cut\b", re.IGNORECASE),
    re.compile(r"\bdo not cut\b", re.IGNORECASE),
    re.compile(r"\bnever cuts?\b", re.IGNORECASE),
]

_ALLOWED_CAMERA_MOVES = [
    "slow handheld arc",
    "single tilt-up", "single tilt up",
    "single tilt-down", "single tilt down",
    "single pan-left", "single pan left",
    "single pan-right", "single pan right",
    "slow push-in", "slow push in",
    "slow pull-out", "slow pull out",
    "slow dolly-left", "slow dolly left",
    "slow dolly-right", "slow dolly right",
    "static close-up", "static close up",
    "handheld follow",
    "locked static",
    "low handheld",
    "overhead static",
    "static wide",
    "static medium",
]
_ALLOWED_CAMERA_MOVES_BY_LEN_DESC = sorted(
    _ALLOWED_CAMERA_MOVES, key=len, reverse=True,
)

_AUDIO_BLOCK_RE = re.compile(r"\bAudio\s*:\s*([^.]*)\.", re.IGNORECASE)

_DEFAULT_AUDIO_BY_CATEGORY = {
    "animals_wildlife": "ambient wind, leaves rustling, distant bird calls",
    "food": "knife on board, simmering pan, ambient kitchen hum",
    "fashion": "fabric rustle, footsteps on floor, distant ambient room tone",
    "cars": "engine idle, tyre on tarmac, distant traffic hum",
    "sports": "footsteps on surface, breath sounds, distant crowd murmur",
    "nature_landscape": "wind through grass, distant water flow, birds in canopy",
    "urban_street": "distant traffic, footsteps on pavement, ambient city hum",
    "indoor_lifestyle": "soft footsteps, ambient room tone, faint clothing rustle",
}
_DEFAULT_AUDIO_FALLBACK = (
    "ambient room tone, soft footsteps, distant environmental hum"
)

_DEFAULT_VERB_SENTENCE_BY_CATEGORY = {
    "animals_wildlife": "The subject moves naturally through the scene.",
    "food": "Hands work steadily, placing each item in turn.",
    "fashion": "The figure walks evenly across the floor.",
    "cars": "The vehicle rolls steadily along the road.",
    "sports": "The athlete runs and turns through the action.",
    "nature_landscape": "Grass sways and leaves rustle across the frame.",
    "urban_street": "People walk past while traffic rolls behind them.",
    "indoor_lifestyle": "The figure walks across the room and sets an item down.",
}
_DEFAULT_VERB_SENTENCE_FALLBACK = "The subject moves naturally through the frame."

_CORE_VERB_RE = re.compile(
    r"\b(walks?|walking|runs?|running|jumps?|jumping|turns?|turning|"
    r"lifts?|lifting|drops?|dropping|pours?|pouring|opens?|opening|"
    r"pushes?|pushing|pulls?|pulling|reaches?|reaching|grabs?|grabbing|"
    r"presses?|pressing|moves?|moving|sets?|setting|places?|placing|"
    r"carries?|carrying|steps?|stepping|leans?|leaning|kneels?|kneeling|"
    r"climbs?|climbing|spins?|spinning|swings?|swinging|nods?|nodding|"
    r"laughs?|laughing|shakes?|shaking|stacks?|stacking|crosses?|crossing|"
    r"writes?|writing|paints?|painting|draws?|drawing|stirs?|stirring|"
    r"scrubs?|scrubbing|cuts?|cutting|wipes?|wiping|folds?|folding|"
    r"taps?|tapping|knocks?|knocking|throws?|throwing|catches?|catching|"
    r"kicks?|kicking|rolls?|rolling|slides?|sliding|paddles?|paddling|"
    r"pedals?|pedalling|pedaling|dribbles?|dribbling|"
    r"explains?|explaining|demonstrates?|demonstrating|"
    r"asks?|asking|answers?|answering|strikes?|striking|"
    r"tightens?|tightening|loosens?|loosening|slips?|slipping|"
    r"recovers?|recovering|passes?|passing)\b",
    re.IGNORECASE,
)


def _strip_double_spaces(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _repair_ai_tell_chars(text: str) -> Tuple[str, List[str]]:
    applied = []
    for src, (dst, name) in _AI_TELL_REPLACEMENTS.items():
        if src in text:
            text = text.replace(src, dst)
            applied.append(f"ai_tell:{name}")
    return text, applied


def _repair_tokenizer_pipeline_leaks(text: str) -> Tuple[str, List[str]]:
    applied = []
    for pat in _TOKENIZER_LEAK_PATTERNS:
        new = pat.sub("", text)
        if new != text:
            applied.append(f"tokenizer_leak:{pat.pattern}")
            text = new
    for pat in _PIPELINE_LEAK_PATTERNS:
        new = pat.sub("", text)
        if new != text:
            applied.append(f"pipeline_leak:{pat.pattern}")
            text = new
    return text, applied


def _repair_hardware(text: str) -> Tuple[str, List[str]]:
    applied = []
    for pat, replacement in _HARDWARE_REWRITES:
        match = pat.search(text)
        if match:
            text = pat.sub(replacement, text)
            applied.append(f"hardware:{match.group(0).lower()}")
    return text, applied


def _repair_multi_shot(text: str) -> Tuple[str, List[str]]:
    applied = []
    for pat, replacement in _MULTI_SHOT_REWRITES:
        match = pat.search(text)
        if match:
            text = pat.sub(replacement, text)
            applied.append(f"multi_shot:{match.group(0).lower()}")
    return text, applied


def _repair_brand_leak(text: str) -> Tuple[str, List[str]]:
    applied = []
    for pat, replacement in _BRAND_REWRITES:
        match = pat.search(text)
        if match:
            text = pat.sub(replacement, text)
            applied.append(f"brand:{match.group(0).lower()}")
    return text, applied


def _repair_marketing_words(text: str) -> Tuple[str, List[str]]:
    applied = []
    matches = _MARKETING_RE.findall(text)
    if matches:
        text = _MARKETING_RE.sub("", text)
        applied.extend(f"marketing:{m.lower()}" for m in matches[:5])
    return text, applied


def _repair_decimal_timestamps(text: str) -> Tuple[str, List[str]]:
    matches = _DECIMAL_TS_RE.findall(text)
    if not matches:
        return text, []
    text = _DECIMAL_TS_RE.sub("", text)
    return text, [f"decimal_ts:{len(matches)}"]


def _repair_forbidden_resolution(text: str) -> Tuple[str, List[str]]:
    applied = []
    suffix_idx = text.rfind(MANDATORY_SUFFIX_STEREO)
    preserved_suffix = ""
    if suffix_idx >= 0:
        preserved_suffix = text[suffix_idx:]
        text = text[:suffix_idx]
    for pat in _FORBIDDEN_RES_PATTERNS:
        match = pat.search(text)
        if match:
            text = pat.sub("", text)
            applied.append(f"forbidden_res:{match.group(0).lower()}")
    return text + preserved_suffix, applied


def _repair_self_contained(text: str) -> Tuple[str, List[str]]:
    matches = _SELF_REF_RE.findall(text)
    if not matches:
        return text, []
    text = _SELF_REF_RE.sub("", text)
    return text, [f"self_contained:{len(matches)}"]


def _repair_negative_phrases(text: str) -> Tuple[str, List[str]]:
    applied = []
    for pat in _NEGATIVE_PHRASE_PATTERNS:
        match = pat.search(text)
        if match:
            text = pat.sub("", text)
            applied.append(f"negative_phrase:{match.group(0).lower()}")
    return text, applied


def _find_camera_move_spans(text: str) -> List[Tuple[int, int, str]]:
    occupied = [False] * len(text)
    found: List[Tuple[int, int, str]] = []
    lowered = text.lower()
    for move in _ALLOWED_CAMERA_MOVES_BY_LEN_DESC:
        start = 0
        while True:
            idx = lowered.find(move, start)
            if idx < 0:
                break
            end = idx + len(move)
            prev_ok = idx == 0 or not text[idx - 1].isalnum()
            next_ok = end == len(text) or not text[end].isalnum()
            if (prev_ok and next_ok
                    and not any(occupied[idx:end])):
                for i in range(idx, end):
                    occupied[i] = True
                canon = move.replace("-", " ")
                found.append((idx, end, canon))
            start = idx + 1
    found.sort(key=lambda t: t[0])
    return found


def _repair_camera_move_multiple(text: str) -> Tuple[str, List[str]]:
    spans = _find_camera_move_spans(text)
    if not spans:
        return text, []
    seen_canonical = set()
    to_delete: List[Tuple[int, int, str]] = []
    for start, end, canon in spans:
        if canon in seen_canonical:
            continue
        if seen_canonical:
            to_delete.append((start, end, canon))
        seen_canonical.add(canon)
    if not to_delete:
        return text, []
    applied = []
    for start, end, canon in reversed(to_delete):
        text = text[:start] + text[end:]
        applied.append(f"camera_move_extra:{canon}")
    return text, applied


def _has_dynamic_verb(text: str) -> bool:
    return bool(_CORE_VERB_RE.search(text))


def _repair_dynamic_verb_missing(
    text: str, category: str,
) -> Tuple[str, List[str]]:
    if _has_dynamic_verb(text):
        return text, []
    sentence = _DEFAULT_VERB_SENTENCE_BY_CATEGORY.get(
        (category or "").lower(), _DEFAULT_VERB_SENTENCE_FALLBACK,
    )
    idx = text.rfind(MANDATORY_SUFFIX_STEREO)
    if idx >= 0:
        text = text[:idx].rstrip() + " " + sentence + " " + text[idx:]
    else:
        text = text.rstrip() + " " + sentence
    return text, ["dynamic_verb:injected"]


def _audio_block_count(text: str) -> int:
    m = _AUDIO_BLOCK_RE.search(text)
    if not m:
        return 0
    parts = re.split(r",|\band\b", m.group(1), flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    return len(parts)


def _repair_audio(text: str, category: str) -> Tuple[str, List[str]]:
    default = _DEFAULT_AUDIO_BY_CATEGORY.get(
        (category or "").lower(), _DEFAULT_AUDIO_FALLBACK,
    )
    m = _AUDIO_BLOCK_RE.search(text)
    if not m:
        injection = f"Audio: {default}."
        idx = text.rfind(MANDATORY_SUFFIX_STEREO)
        if idx >= 0:
            text = text[:idx].rstrip() + " " + injection + " " + text[idx:]
        else:
            text = text.rstrip() + " " + injection
        return text, ["audio:injected"]
    parts = re.split(r",|\band\b", m.group(1), flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 3:
        return text, []
    pad_pool = [p.strip() for p in default.split(",")]
    pad_pool = [
        p for p in pad_pool
        if p and p.lower() not in {x.lower() for x in parts}
    ]
    while len(parts) < 3 and pad_pool:
        parts.append(pad_pool.pop(0))
    new_block = "Audio: " + ", ".join(parts) + "."
    text = text[:m.start()] + new_block + text[m.end():]
    return text, ["audio:padded"]


def _repair_mandatory_suffix(text: str) -> Tuple[str, List[str]]:
    if MANDATORY_SUFFIX_STEREO in text:
        return text, []
    near_miss = re.compile(r"\b1920\s?[x\u00d7]\s?1080\b[^.]*\.", re.IGNORECASE)
    text = near_miss.sub("", text).rstrip()
    if text and not text.endswith("."):
        text += "."
    text = (text + " " + MANDATORY_SUFFIX_STEREO).strip()
    return text, ["mandatory_suffix:appended"]


_BAND_BY_STYLE = {
    "casual": (150, 220),
    "precise": (180, 240),
    "narrative": (200, 280),
    "terse": (80, 140),
    "exhaustive": (240, 280),
    "creative": (180, 240),
}
_DEFAULT_BAND = (180, 240)

_PAD_SENTENCE = (
    "The take continues in a single steady recording across the full "
    "duration with no edits or staging beyond what is already in the frame."
)
_TERSE_PAD_SENTENCE = "The take continues steadily across the full duration."

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _word_count(text: str) -> int:
    return len([w for w in text.split() if w])


def _is_protected_sentence(sentence: str) -> bool:
    if _CORE_VERB_RE.search(sentence):
        return True
    if _AUDIO_BLOCK_RE.search(sentence):
        return True
    lowered = sentence.lower()
    for move in _ALLOWED_CAMERA_MOVES_BY_LEN_DESC:
        if move in lowered:
            return True
    return False


def _repair_word_count_band(text: str, style: str) -> Tuple[str, List[str]]:
    style_norm = (style or "").strip().lower()
    lo, hi = _BAND_BY_STYLE.get(style_norm, _DEFAULT_BAND)

    suffix_idx = text.rfind(MANDATORY_SUFFIX_STEREO)
    if suffix_idx >= 0:
        body = text[:suffix_idx].rstrip()
        suffix = text[suffix_idx:]
    else:
        body = text.rstrip()
        suffix = ""

    suffix_wc = _word_count(suffix)
    body_wc = _word_count(body)
    total = body_wc + suffix_wc
    original_total = total

    if lo <= total <= hi:
        return text, []

    if total > hi:
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(body) if s.strip()]
        if not sentences:
            return text, []
        candidates = sorted(
            [(i, s) for i, s in enumerate(sentences)
             if not _is_protected_sentence(s)],
            key=lambda t: -_word_count(t[1]),
        )
        dropped = set()
        cur_total = total
        for i, sentence in candidates:
            if cur_total <= hi:
                break
            sw = _word_count(sentence)
            if cur_total - sw < lo:
                continue
            dropped.add(i)
            cur_total -= sw
        if not dropped or cur_total == total:
            return text, []
        new_body = " ".join(
            s for i, s in enumerate(sentences) if i not in dropped
        ).strip()
        if not new_body:
            return text, []
        rebuilt = (new_body + " " + suffix).strip() if suffix else new_body
        tag = f"word_count_high:trimmed_from_{original_total}_to_{cur_total}"
        return rebuilt, [tag]

    pad_sentence = _TERSE_PAD_SENTENCE if style_norm == "terse" else _PAD_SENTENCE
    pad_wc = _word_count(pad_sentence)
    cur_total = total
    safety = 30
    while cur_total < lo and safety > 0:
        if cur_total + pad_wc > hi:
            break
        body = body.rstrip() + " " + pad_sentence
        cur_total += pad_wc
        safety -= 1
    if cur_total == total:
        return text, []
    rebuilt = (body + " " + suffix).strip() if suffix else body.strip()
    tag = f"word_count_low:padded_from_{original_total}_to_{cur_total}"
    return rebuilt, [tag]


def repair_all(
    text: str, category: str = "", style: str = "",
) -> Tuple[str, List[str]]:
    if not text:
        return text, []
    applied: List[str] = []
    cat = (category or "").strip().lower()

    for fn in (
        _repair_ai_tell_chars,
        _repair_tokenizer_pipeline_leaks,
        _repair_decimal_timestamps,
        _repair_forbidden_resolution,
        _repair_negative_phrases,
        _repair_self_contained,
        _repair_marketing_words,
        _repair_brand_leak,
        _repair_hardware,
        _repair_multi_shot,
        _repair_camera_move_multiple,
    ):
        text, changes = fn(text)
        applied.extend(changes)

    text, changes = _repair_audio(text, cat)
    applied.extend(changes)
    text, changes = _repair_dynamic_verb_missing(text, cat)
    applied.extend(changes)

    text, changes = _repair_mandatory_suffix(text)
    applied.extend(changes)

    text, changes = _repair_word_count_band(text, style)
    applied.extend(changes)

    text = _strip_double_spaces(text)
    return text, applied
