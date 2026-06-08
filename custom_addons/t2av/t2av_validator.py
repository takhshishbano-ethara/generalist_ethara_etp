"""
T2AV enriched_prompt deterministic post-generation validator.

Pairs with `T2AV Enrichment System Prompt.md`. The system prompt does the
generation, this module does the hard-fail. Pipeline shape:

    enriched_prompt = llm.generate(system_prompt, user_row)
    report = validate(enriched_prompt, row)
    if report.fatal:
        reject_or_regenerate(...)
    elif report.warnings:
        flag_for_review(...)
    else:
        accept(...)

Stdlib only. No regex flavour beyond `re`. Every check is deterministic and
side-effect free. Every rule maps back to a measured defect in Tasks.csv
(see audit history in the spec doc).

Severity model
--------------
- FATAL:   reject the prompt, regenerate. These are spec violations or
           contract violations that ship a broken row downstream.
- WARNING: human-review flag. The prompt is plausibly fine but the model
           drifted on an advisory rule.
- INFO:    statistic only, never blocks.

Run as a CLI for spot checks:

    python t2av_validator.py --prompt "..."        # one prompt from stdin or arg
    python t2av_validator.py --csv path.csv        # enrich-and-validate batch
    python t2av_validator.py --jsonl path.jsonl    # {"prompt": "...", "style": "casual", "category": "..."}

Exit code is 0 if all rows pass FATAL, 1 if any FATAL.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Iterable


DRIFT_NOTE_SPLIT_RE = re.compile(r"\n\s*\n\s*>?\s*Drift\s*note:", re.IGNORECASE)


def split_drift_note(enriched: str) -> tuple[str, str]:
    if not enriched:
        return "", ""
    m = DRIFT_NOTE_SPLIT_RE.search(enriched)
    if not m:
        return enriched.strip(), ""
    return enriched[: m.start()].strip(), enriched[m.start():].strip()


MANDATORY_SUFFIX_STEREO = (
    "1920x1080 at 30 fps, clean handheld framing, "
    "natural colour, in-camera audio at 48 kHz stereo."
)
MANDATORY_SUFFIX_MONO = (
    "1920x1080 at 30 fps, clean handheld framing, "
    "natural colour, in-camera audio at 48 kHz mono."
)

# Per-style word bands from the system prompt.
STYLE_WORD_BANDS = {
    "casual":     (150, 220),
    "precise":    (180, 240),
    "narrative":  (200, 280),
    "terse":      (80,  140),
    "exhaustive": (240, 280),
    "creative":   (180, 240),
}

# Absolute outer envelope. If style is missing or unknown, fall back to this.
GLOBAL_WORD_BAND = (80, 280)

# AI-tell punctuation. Hard ban from Round 4.
AI_TELL_CHARS = {
    "\u2014": "em-dash",
    "\u2013": "en-dash",
    "\u00d7": "multiplication",
    "\u2026": "ellipsis-char",
    "\u2018": "left-single-quote",
    "\u2019": "right-single-quote",
    "\u201c": "left-double-quote",
    "\u201d": "right-double-quote",
}

# Tokenizer / chat-template leakage. From Round 3 audit: 31 garbage rows.
TOKENIZER_LEAKS = [
    r"<\|start\|>",
    r"<\|end\|>",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"\bassistantassistant\b",
    r"\bto=self\b",
    r"\bto=selfself\b",
    r"\[SUB-TYPE\]",
    r"\[TOPIC\]",
    r"\[CATEGORY\]",
    r"\bSUB-TYPE\b",
]

# Annotator / pipeline / dataset leakage. From Round 3: 168 prompts (1.25%).
PIPELINE_LEAKS = [
    r"\bannotator\b",
    r"\bvendor[- ]facing\b",
    r"\bthird[- ]party vendor\b",
    r"\bvendor (?:sdk|api|integration|tool|platform|prompt|model|pipeline)\b",
    r"\bdataset\b",
    r"\btraining sample\b",
    r"\bcategory\s*:",
    r"\bsub[_\- ]?category\s*:",
    r"\bmeta[_\- ]?prompt\b",
    r"\bGenerate dataset\b",
    r"\b1080p training\b",
    r"\bcollection sample\b",
]

# Brand / IP / trademark leakage. From Round 3: 719 prompts (5.36%).
# Word-boundary matched, case-insensitive. Order doesn't matter.
BRAND_TERMS = [
    "olympic", "olympics", "times square", "tiktok", "apple inc",
    "broadway", "pixar", "instagram", "youtube", "star wars", "marvel",
    "disney", "spacex", "lego", "tesla", "eiffel tower", "facebook",
    "nba", "mlb", "nfl", "fifa", "samsung", "nike", "mickey mouse",
    "coca-cola", "coca cola", "mcdonald", "mcdonalds", "starbucks",
    "kodachrome", "hollywood sign", "snoopy", "macy", "macys", "macy's",
    "adidas", "sony", "snapchat", "anamorphic", "imax", "arri alexa",
    "red komodo", "red dragon", "red helium", "prores",
]

# Marketing adjectives. Mode-collapse fingerprint of LLM creative writing.
MARKETING_WORDS = [
    "stunning", "breathtaking", "epic", "mesmerising", "mesmerizing",
    "captivating", "unforgettable", "immersive", "majestic", "ethereal",
    "otherworldly", "magical", "awe-inspiring", "spellbinding",
    "enchanting", "transcendent",
]

# Specialised cinema hardware. 8-25s commodity capture has no business
# requesting an Arri or a Steadicam. Round 3: 1,055 prompts (7.9%).
HARDWARE_TERMS = [
    r"\bdrone\b",
    r"\baerial\b",
    r"\bcrane shot\b",
    r"\banamorphic\b",
    r"\bimax\b",
    r"\b90\s?mm\b",
    r"\b70\s?mm\b",
    r"\bRED\s+(camera|komodo|dragon|helium)\b",
    r"\barri(?:\s+alexa)?\b",
    r"\bprores\b",
    r"\bdolly track\b",
    r"\bsteadicam\b",
    r"\bsteady\s?cam\b",
    r"\bgimbal rig\b",
]

# Multi-shot / edit-verb leakage. Spec is single continuous take.
# Round 3: 9.06% of corpus. This breaks SeedDance 2's one-shot model.
MULTI_SHOT_VERBS = [
    r"\bcuts? to\b",
    r"\bmatch cut\b",
    r"\bjump cut\b",
    r"\bsmash cut\b",
    r"\bfades? to\b",
    r"\bcrossfade\b",
    r"\bdissolves? to\b",
    r"\btransition(?:s|ing)? to\b",
    r"\bmontage\b",
    r"\bthen we see\b",
    r"\bthen it shows\b",
    r"\bnext shot\b",
    r"\bcut back to\b",
    r"\bsplit screen\b",  # warning, not fatal: spec sample 7 had this on purpose
]

# Decimal timestamps. Spec wants action-anchored language, not t=3.5s.
DECIMAL_TIMESTAMP_RE = re.compile(
    r"\bt\s*=\s*\d+(?:\.\d+)?\s*s?\b",
    re.IGNORECASE,
)

# Resolutions / formats that contradict the spec. 4K/8K/UHD are FATAL.
FORBIDDEN_RES = [
    r"\b4\s?K\b",
    r"\b8\s?K\b",
    r"\bUHD\b",
    r"\bUltra\s?HD\b",
    r"\b3840\s?[x\u00d7]\s?2160\b",
    r"\b7680\s?[x\u00d7]\s?4320\b",
    r"\bcinematic texture\b",
    r"\bsoft film grain\b",
    r"\bgrain overlay\b",
]

# Allowed camera moves. Exactly one must appear. From the system prompt.
ALLOWED_CAMERA_MOVES = [
    "locked static",
    "slow push-in",
    "slow push in",
    "slow pull-out",
    "slow pull out",
    "slow dolly-left",
    "slow dolly left",
    "slow dolly-right",
    "slow dolly right",
    "slow handheld arc",
    "low handheld",
    "overhead static",
    "single tilt-up",
    "single tilt up",
    "single tilt-down",
    "single tilt down",
    "single pan-left",
    "single pan left",
    "single pan-right",
    "single pan right",
    # Conservative additions: simple one-axis moves the spec accepts.
    "static wide",
    "static medium",
    "static close-up",
    "static close up",
    "handheld follow",
]

# Dynamic verbs the prompt should contain at least one of.
# From Round 2 audit: 56.13% of corpus had ZERO dynamic verbs.
DYNAMIC_VERBS = [
    "walks", "walk", "walking",
    "runs", "run", "running",
    "jumps", "jump", "jumping",
    "leans", "lean", "leaning",
    "kneels", "kneel", "kneeling",
    "turns", "turn", "turning",
    "lifts", "lift", "lifting",
    "drops", "drop", "dropping",
    "pours", "pour", "pouring",
    "scrubs", "scrub", "scrubbing",
    "cuts", "cut", "cutting",
    "stirs", "stir", "stirring",
    "opens", "open", "opening",
    "shuts", "shut", "shutting",
    "pushes", "push", "pushing",
    "pulls", "pull", "pulling",
    "throws", "throw", "throwing",
    "catches", "catch", "catching",
    "kicks", "kick", "kicking",
    "swings", "swing", "swinging",
    "spins", "spin", "spinning",
    "climbs", "climb", "climbing",
    "rolls", "roll", "rolling",
    "slides", "slide", "sliding",
    "writes", "write", "writing",
    "draws", "draw", "drawing",
    "paints", "paint", "painting",
    "reaches", "reach", "reaching",
    "grabs", "grab", "grabbing",
    "presses", "press", "pressing",
    "taps", "tap", "tapping",
    "knocks", "knock", "knocking",
    "wipes", "wipe", "wiping",
    "folds", "fold", "folding",
    "sets", "set", "setting",
    "places", "place", "placing",
    "carries", "carry", "carrying",
    "passes", "pass", "passing",
    "pedals", "pedal", "pedaling", "pedalling",
    "paddles", "paddle", "paddling",
    "dribbles", "dribble", "dribbling",
    "explains", "explain", "explaining",
    "writes", "demonstrates", "demonstrate", "demonstrating",
    "asks", "ask", "asking",
    "answers", "answer", "answering",
    "laughs", "laugh", "laughing",
    "nods", "nod", "nodding",
    "shakes", "shake", "shaking",
    "stacks", "stack", "stacking",
    "crosses", "cross", "crossing",
    "steps", "step", "stepping",
    "slips", "slip", "slipping",
    "recovers", "recover", "recovering",
    "strikes", "strike", "striking",
    "tightens", "tighten", "tightening",
    "loosens", "loosen", "loosening",
]

# Mood-tag stacks. Three or more hedge adjectives in a row is the LLM
# ambient-texture filler tic. Round 2 audit confirmed this is endemic.
HEDGE_ADJECTIVES = {
    "soft", "low", "distant", "faint", "gentle", "subtle", "quiet",
    "muted", "hushed", "dim", "warm", "cool", "still", "calm",
    "barely", "almost",
}

# Generic wardrobe. Replace with a named garment.
GENERIC_WARDROBE_RE = re.compile(
    r"\b(casual clothes|casual outfit|casual attire|"
    r"normal clothes|regular clothes|street clothes|"
    r"everyday wear|generic outfit)\b",
    re.IGNORECASE,
)

# Negation phrasing that should have been rewritten to positive. Spec
# rule from Round 4. Not all are FATAL, but they're style violations.
NEGATIVE_PHRASES = [
    r"\bno cuts\b",
    r"\bno time-?lapse\b",
    r"\bno music\b",
    r"\bnot a\b.*\bshot\b",
    r"\bwithout any cuts\b",
    r"\bdon'?t cut\b",
    r"\bdo not cut\b",
    r"\bnever cuts?\b",
]

# Audio block. Must be present and have >=3 elements.
AUDIO_BLOCK_RE = re.compile(r"\bAudio\s*:\s*([^.]*)\.", re.IGNORECASE)

# Quoted dialogue.
DIALOGUE_RE = re.compile(r'"([^"]+)"')


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    evidence: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    prompt: str
    word_count: int = 0
    style: str | None = None
    category: str | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def fatal(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "FATAL"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "WARNING"]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "INFO"]

    @property
    def passed(self) -> bool:
        return not self.fatal

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "word_count": self.word_count,
            "style": self.style,
            "category": self.category,
            "passed": self.passed,
            "findings": [f.to_dict() for f in self.findings],
        }


def _wc(text: str) -> int:
    return len(re.findall(r"\b\w[\w'-]*\b", text))


def _add(report: Report, rule: str, severity: str, msg: str, ev: str = "") -> None:
    report.findings.append(Finding(rule, severity, msg, ev))


def _check_mandatory_suffix(prompt: str, report: Report) -> None:
    # Suffix can be either stereo or mono. Trailing whitespace tolerated.
    body = prompt.rstrip()
    if body.endswith(MANDATORY_SUFFIX_STEREO) or body.endswith(MANDATORY_SUFFIX_MONO):
        return
    # Diagnose what went wrong.
    if "1920x1080" not in body:
        _add(report, "suffix.resolution", "FATAL",
             "Mandatory final sentence missing or wrong resolution. "
             "Must end with the spec-honest suffix.",
             ev=body[-120:])
        return
    if "48 kHz" not in body:
        _add(report, "suffix.audio_format", "FATAL",
             "Mandatory final sentence missing 48 kHz declaration.",
             ev=body[-120:])
        return
    _add(report, "suffix.exact_match", "FATAL",
         "Final sentence is close but not verbatim. The system prompt "
         "requires byte-exact match (stereo or mono variant only).",
         ev=body[-160:])


def _check_ai_tell_chars(prompt: str, report: Report) -> None:
    for ch, name in AI_TELL_CHARS.items():
        if ch in prompt:
            ctx_idx = prompt.find(ch)
            ev = prompt[max(0, ctx_idx - 30):ctx_idx + 30]
            _add(report, f"ai_tell.{name}", "FATAL",
                 f"Forbidden character {name!r} ({ch!r}) found. "
                 f"Use ASCII punctuation only.",
                 ev=ev)


def _check_tokenizer_leak(prompt: str, report: Report) -> None:
    for pat in TOKENIZER_LEAKS:
        m = re.search(pat, prompt, re.IGNORECASE)
        if m:
            _add(report, "tokenizer_leak", "FATAL",
                 "Chat-template / tokenizer artefact leaked into prompt.",
                 ev=m.group(0))


def _check_pipeline_leak(prompt: str, report: Report) -> None:
    for pat in PIPELINE_LEAKS:
        m = re.search(pat, prompt, re.IGNORECASE)
        if m:
            _add(report, "pipeline_leak", "FATAL",
                 "Pipeline / annotator language leaked. "
                 "Vendor-facing prompt must read as a real user request.",
                 ev=m.group(0))


def _check_brand_leak(prompt: str, report: Report) -> None:
    for term in BRAND_TERMS:
        pat = r"\b" + re.escape(term) + r"\b"
        m = re.search(pat, prompt, re.IGNORECASE)
        if m:
            _add(report, "brand_leak", "FATAL",
                 f"Branded / trademarked term '{term}' found. "
                 f"Substitute with a generic equivalent.",
                 ev=m.group(0))


def _check_marketing_words(prompt: str, report: Report) -> None:
    found = []
    for w in MARKETING_WORDS:
        pat = r"\b" + re.escape(w) + r"\b"
        if re.search(pat, prompt, re.IGNORECASE):
            found.append(w)
    if found:
        _add(report, "marketing_adjectives", "FATAL",
             "Marketing-deck adjectives found. Specific physical detail wanted.",
             ev=", ".join(found))


_DRONE_AUDIO_CONTEXT = re.compile(
    r"(?:engine|insect|insects?|rhythmic|low|deep|distant|constant|steady|"
    r"throaty|throat|baritone|warm|sustained|mechanical|sub-?bass|sub|"
    r"bass|hum|hummed|humming|tone|tonal|note|monotone|piston|radial|"
    r"propeller|propellers?|cicadas?|crickets?|mosquitoes?|fly|flies|fan|"
    r"motor|machine|amp|amplifier|tower|fluorescent|refrigerator|"
    r"transformer|generator|appliance|sound|audio|chant|chorus)"
    r"[\s,;:.()/-]+(?:[a-z'-]+\s+){0,4}drone\b",
    re.IGNORECASE,
)


def _is_audio_drone(prompt: str, match: re.Match) -> bool:
    """True if the matched 'drone' refers to a tonal sound, not a UAV.

    Two signals: (1) appears inside an 'Audio:' block, (2) preceded by an
    audio-context word within ~4 tokens. Either is sufficient.
    """
    word = match.group(0).lower()
    if word != "drone":
        return False
    start = match.start()
    # Audio: block heuristic. Find the closest preceding 'Audio:' label and
    # check that the match sits before the next sentence-ending period that
    # starts a non-audio sentence.
    body = prompt
    audio_idx = body.rfind("Audio:", 0, start)
    if audio_idx >= 0:
        # Anything between Audio: and the end (or next sentence break) counts.
        segment_end = body.find("\n\n", audio_idx)
        if segment_end == -1:
            segment_end = len(body)
        if audio_idx <= start < segment_end:
            return True
    # Phrase-level context: "<audio-word> ... drone" within 4 tokens.
    window_start = max(0, start - 60)
    window = body[window_start:start + len("drone")]
    if _DRONE_AUDIO_CONTEXT.search(window):
        return True
    return False


def _check_hardware(prompt: str, report: Report) -> None:
    for pat in HARDWARE_TERMS:
        for m in re.finditer(pat, prompt, re.IGNORECASE):
            # Audio-context "drone" (engine drone, insect drone, low drone) is
            # a tonal sound effect, not an aerial UAV. Skip those.
            if pat == r"\bdrone\b" and _is_audio_drone(prompt, m):
                continue
            _add(report, "specialised_hardware", "FATAL",
                 "Specialised cinema hardware mentioned. "
                 "8-25s commodity capture, not a feature shoot.",
                 ev=m.group(0))
            break  # one FATAL per pattern is enough


def _check_multi_shot(prompt: str, report: Report) -> None:
    for pat in MULTI_SHOT_VERBS:
        m = re.search(pat, prompt, re.IGNORECASE)
        if not m:
            continue
        sev = "WARNING" if "split screen" in pat else "FATAL"
        _add(report, "multi_shot_verb", sev,
             "Multi-shot edit verb in single-take prompt. "
             "SeedDance 2 generates one continuous take.",
             ev=m.group(0))


def _check_decimal_timestamp(prompt: str, report: Report) -> None:
    m = DECIMAL_TIMESTAMP_RE.search(prompt)
    if m:
        _add(report, "decimal_timestamp", "FATAL",
             "Decimal timestamp anchor found. "
             "Anchor sound to the action, not to a timecode.",
             ev=m.group(0))


def _check_forbidden_resolution(prompt: str, report: Report) -> None:
    body = prompt
    # Strip the mandatory suffix before scanning, otherwise '1920x1080' is fine.
    for suffix in (MANDATORY_SUFFIX_STEREO, MANDATORY_SUFFIX_MONO):
        if body.rstrip().endswith(suffix):
            body = body.rstrip()[: -len(suffix)]
            break
    for pat in FORBIDDEN_RES:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            _add(report, "forbidden_resolution", "FATAL",
                 "Resolution / texture term contradicts spec "
                 "(720p min, 1080p preferred). "
                 "Mandatory suffix already declares 1920x1080.",
                 ev=m.group(0))


def _check_negatives(prompt: str, report: Report) -> None:
    for pat in NEGATIVE_PHRASES:
        m = re.search(pat, prompt, re.IGNORECASE)
        if m:
            _add(report, "negative_phrasing", "WARNING",
                 "Negative phrasing should be rewritten as positive. "
                 "SeedDance ignores negatives.",
                 ev=m.group(0))


def _check_generic_wardrobe(prompt: str, report: Report) -> None:
    m = GENERIC_WARDROBE_RE.search(prompt)
    if m:
        _add(report, "generic_wardrobe", "WARNING",
             "Generic wardrobe phrase. Replace with a named garment "
             "(grey hoodie, navy work apron, etc.).",
             ev=m.group(0))


RUNAWAY_THRESHOLD = 380


def _check_word_count(prompt: str, report: Report) -> None:
    wc = _wc(prompt)
    report.word_count = wc
    style = (report.style or "").strip().lower()
    band = STYLE_WORD_BANDS.get(style, GLOBAL_WORD_BAND)
    lo, hi = band
    if wc < lo:
        _add(report, "word_count.low", "WARNING",
             f"Word count {wc} below band {lo}-{hi} for style '{style or 'unknown'}'.")
    elif wc > hi:
        sev = "FATAL" if wc > RUNAWAY_THRESHOLD else "WARNING"
        _add(report, "word_count.high", sev,
             f"Word count {wc} above band {lo}-{hi} for style '{style or 'unknown'}'.")
    if wc > RUNAWAY_THRESHOLD:
        _add(report, "word_count.runaway", "FATAL",
             f"Runaway word count {wc}. Possible meta-prompt regurgitation.")


def _camera_move_class(move: str) -> str:
    m = move.replace("-", " ")
    if "static" in m:
        return "static"
    if "push in" in m or "pull out" in m:
        return "dolly_z"
    if "dolly left" in m or "dolly right" in m:
        return "dolly_xy"
    if "handheld arc" in m:
        return "arc"
    if "low handheld" in m or "handheld follow" in m:
        return "handheld"
    if "tilt up" in m or "tilt down" in m:
        return "tilt"
    if "pan left" in m or "pan right" in m:
        return "pan"
    return m


def _check_camera_move(prompt: str, report: Report) -> None:
    text_lc = prompt.lower()
    moves_sorted = sorted(ALLOWED_CAMERA_MOVES, key=len, reverse=True)
    spans: list[tuple[int, int, str]] = []
    for move in moves_sorted:
        start = 0
        while True:
            idx = text_lc.find(move, start)
            if idx == -1:
                break
            end = idx + len(move)
            if any(s <= idx and end <= e for s, e, _ in spans):
                start = end
                continue
            spans.append((idx, end, move))
            start = end
    classes = {_camera_move_class(m) for _, _, m in spans}
    if not classes:
        _add(report, "camera_move.missing", "WARNING",
             "No allowed camera move declared. Pick exactly one from the "
             "system-prompt list (locked static / slow push-in / etc.).")
    elif len(classes) > 1:
        canonical = {m.replace("-", " ") for _, _, m in spans}
        _add(report, "camera_move.multiple", "FATAL",
             "More than one camera move declared. SeedDance 2 needs exactly one.",
             ev=", ".join(sorted(canonical)))


def _check_audio_block(prompt: str, report: Report) -> None:
    m = AUDIO_BLOCK_RE.search(prompt)
    if not m:
        # Spec §4.2: every prompt must explicitly mention or imply audio.
        # Quoted dialogue counts as an implicit audio block for dialogue category.
        if not DIALOGUE_RE.search(prompt):
            _add(report, "audio.missing", "FATAL",
                 "No 'Audio:' block and no quoted dialogue. "
                 "Spec §4.2 requires audio in every prompt.")
        else:
            _add(report, "audio.implicit_only", "WARNING",
                 "No explicit 'Audio:' block. Dialogue alone is allowed for "
                 "multi_speaker_dialogue but explicit Audio: is preferred.")
        return
    body = m.group(1)
    # Count comma- or 'and'-separated elements as a rough proxy.
    parts = re.split(r",|\band\b", body)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 3:
        _add(report, "audio.too_few", "WARNING",
             f"Audio block has only {len(parts)} elements. "
             f"System prompt asks for ≥3 distinct sound sources.",
             ev=body.strip())


def _check_dynamic_verb(prompt: str, report: Report) -> None:
    text_lc = prompt.lower()
    # Strip suffix to avoid counting nothing useful.
    for suffix in (MANDATORY_SUFFIX_STEREO, MANDATORY_SUFFIX_MONO):
        if text_lc.endswith(suffix.lower()):
            text_lc = text_lc[: -len(suffix)]
            break
    # \b boundary needed otherwise 'stir' matches 'stirring' twice etc.,
    # but for this binary present/absent check we just need any hit.
    for v in DYNAMIC_VERBS:
        if re.search(r"\b" + re.escape(v) + r"\b", text_lc):
            return
    _add(report, "dynamic_verb.missing", "FATAL",
         "No dynamic verb found. Round 2 audit: 56% of corpus failed this. "
         "Add at least one action verb tied to a visible motion.")


def _check_hedge_stack(prompt: str, report: Report) -> None:
    """Three or more hedge adjectives within a 6-word window = mood-tag stack."""
    tokens = re.findall(r"[a-zA-Z']+", prompt.lower())
    flagged_window = None
    for i in range(len(tokens)):
        window = tokens[i : i + 6]
        hits = [t for t in window if t in HEDGE_ADJECTIVES]
        if len(hits) >= 3:
            flagged_window = " ".join(window)
            break
    if flagged_window:
        _add(report, "hedge_stack", "WARNING",
             "Three or more hedge adjectives within a 6-word window "
             "(soft / low / distant / faint / gentle, etc.). "
             "Pick one specific texture instead.",
             ev=flagged_window)


def _check_dialogue_rules(prompt: str, report: Report) -> None:
    """multi_speaker_dialogue category needs ≥2 quoted lines, ≥4 words each."""
    cat = (report.category or "").strip().lower()
    if cat != "multi_speaker_dialogue":
        return
    lines = DIALOGUE_RE.findall(prompt)
    if len(lines) < 2:
        _add(report, "dialogue.too_few_lines", "FATAL",
             f"multi_speaker_dialogue requires ≥2 quoted lines, found {len(lines)}.")
        return
    short = [ln for ln in lines if len(ln.strip().split()) < 4]
    if short:
        _add(report, "dialogue.line_too_short", "WARNING",
             "Quoted dialogue line under 4 words. "
             "Spec wants substantive utterances.",
             ev=" | ".join(short))


def _check_self_contained(prompt: str, report: Report) -> None:
    """Prompt should not reference rows / sub-types / metadata."""
    if re.search(r"\b(row|item)\s*#?\s*\d+\b", prompt, re.IGNORECASE):
        _add(report, "self_contained", "FATAL",
             "Prompt references a row or item number. "
             "Must be self-contained natural-user prose.")


def _check_meta_prompt_leak(prompt: str, report: Report) -> None:
    """Catch the 2,243-word meta-prompt regurgitation pattern from Round 3."""
    # Heuristics: very high length plus directive language.
    wc = report.word_count or _wc(prompt)
    if wc > 280 and re.search(
        r"\b(you are|your task|the model should|generate a|create a prompt)\b",
        prompt, re.IGNORECASE,
    ):
        _add(report, "meta_prompt_leak", "FATAL",
             "Suspected meta-prompt regurgitation: long output with "
             "instruction-language verbs.")


def _check_emoji_or_nonprintable(prompt: str, report: Report) -> None:
    # Strip CR/LF/tab from the printable test.
    cleaned = prompt.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    bad = [c for c in cleaned if ord(c) > 0x7F and c not in AI_TELL_CHARS]
    if bad:
        # Just take the unique offenders.
        uniq = sorted({c for c in bad})
        _add(report, "non_ascii", "WARNING",
             "Non-ASCII character(s) detected. ASCII-only prompts ship cleanest.",
             ev="".join(uniq[:10]))


def validate(
    prompt: str,
    *,
    style: str | None = None,
    category: str | None = None,
) -> Report:
    """
    Run every deterministic check against a single enriched_prompt.

    Returns a Report with `.fatal`, `.warnings`, `.infos`, and `.passed`.
    """
    if not isinstance(prompt, str):
        raise TypeError("prompt must be str")
    report = Report(prompt=prompt, style=style, category=category)

    # word_count is set inside _check_word_count; do it first so other
    # rules can read report.word_count.
    _check_word_count(prompt, report)

    _check_mandatory_suffix(prompt, report)
    _check_ai_tell_chars(prompt, report)
    _check_tokenizer_leak(prompt, report)
    _check_pipeline_leak(prompt, report)
    _check_brand_leak(prompt, report)
    _check_marketing_words(prompt, report)
    _check_hardware(prompt, report)
    _check_multi_shot(prompt, report)
    _check_decimal_timestamp(prompt, report)
    _check_forbidden_resolution(prompt, report)
    _check_negatives(prompt, report)
    _check_generic_wardrobe(prompt, report)
    _check_camera_move(prompt, report)
    _check_audio_block(prompt, report)
    _check_dynamic_verb(prompt, report)
    _check_hedge_stack(prompt, report)
    _check_dialogue_rules(prompt, report)
    _check_self_contained(prompt, report)
    _check_meta_prompt_leak(prompt, report)
    _check_emoji_or_nonprintable(prompt, report)

    return report


def validate_batch(rows: Iterable[dict]) -> list[Report]:
    """rows is an iterable of dicts with keys: prompt, style?, category?."""
    out = []
    for row in rows:
        p = row.get("prompt") or row.get("enriched_prompt") or ""
        out.append(validate(
            p,
            style=row.get("style") or row.get("Style"),
            category=row.get("category") or row.get("Category"),
        ))
    return out


def _format_report_text(r: Report) -> str:
    lines = []
    status = "PASS" if r.passed else "FAIL"
    lines.append(f"[{status}] words={r.word_count} "
                 f"style={r.style or '-'} category={r.category or '-'}")
    for f in r.findings:
        ev = f" :: {f.evidence}" if f.evidence else ""
        lines.append(f"  {f.severity:7s} {f.rule:30s} {f.message}{ev}")
    return "\n".join(lines)


def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt", help="Validate a single prompt string.")
    g.add_argument("--csv",    help="CSV with at minimum a 'prompt' column.")
    g.add_argument("--jsonl",  help="JSONL, one row per line, key 'prompt'.")
    p.add_argument("--style",    help="Style hint when --prompt is used.")
    p.add_argument("--category", help="Category hint when --prompt is used.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of text.")
    args = p.parse_args()

    reports: list[Report] = []
    if args.prompt:
        prompt = args.prompt
        if prompt == "-":
            prompt = sys.stdin.read()
        reports.append(validate(prompt, style=args.style, category=args.category))
    elif args.csv:
        with open(args.csv, newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for row in rd:
                p_text = row.get("enriched_prompt") or row.get("prompt") or ""
                reports.append(validate(
                    p_text,
                    style=row.get("Style") or row.get("style"),
                    category=row.get("Category") or row.get("category"),
                ))
    elif args.jsonl:
        with open(args.jsonl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                p_text = row.get("enriched_prompt") or row.get("prompt") or ""
                reports.append(validate(
                    p_text,
                    style=row.get("style") or row.get("Style"),
                    category=row.get("category") or row.get("Category"),
                ))

    if args.json:
        json.dump([r.to_dict() for r in reports], sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        for r in reports:
            print(_format_report_text(r))
            print()
        n = len(reports)
        fails = sum(1 for r in reports if not r.passed)
        warns = sum(1 for r in reports if r.warnings and r.passed)
        print(f"Summary: {n} prompts, {fails} FAIL, {warns} WARN-only, "
              f"{n - fails - warns} clean.")

    return 1 if any(not r.passed for r in reports) else 0


if __name__ == "__main__":
    sys.exit(_cli())
