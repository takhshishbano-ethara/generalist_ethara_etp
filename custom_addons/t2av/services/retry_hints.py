from __future__ import annotations


RULE_HINTS: dict[str, str] = {
    "camera_move.multiple": (
        "STRICT CORRECTION: declare EXACTLY ONE camera move in the Camera block. "
        "Do NOT list two. Pick a single move from the allowed list and stick with it."
    ),
    "word_count.high": (
        "STRICT CORRECTION: keep the paragraph within the word band for this style. "
        "Cut filler. Do not exceed 280 words."
    ),
    "word_count.runaway": (
        "STRICT CORRECTION: keep the paragraph well under 320 words. Tighten prose."
    ),
    "dynamic_verb.missing": (
        "STRICT CORRECTION: include at least one strong dynamic verb describing "
        "human or object motion (e.g. lifts, spins, hammers, leaps, dives)."
    ),
    "pipeline_leak": (
        "STRICT CORRECTION: never write annotator, training, dataset, sample, "
        "pipeline, prompt, model, vendor, or any meta language. Only describe the "
        "scene itself."
    ),
    "multi_shot_verb": (
        "STRICT CORRECTION: write ONE continuous shot. Do not use cut to, fade to, "
        "transition, dissolve, or any edit verb. The whole paragraph is one take."
    ),
    "brand_leak": (
        "STRICT CORRECTION: use only generic descriptors. No brand names, no "
        "celebrity names, no real-world venue names (no Times Square, Olympic, "
        "TikTok, Disney, Pixar, Hollywood, etc.)."
    ),
    "audio.missing": (
        "STRICT CORRECTION: end the Audio block with at least three distinct "
        "diegetic sound elements separated by commas."
    ),
    "meta_prompt_leak": (
        "STRICT CORRECTION: do not echo prompt instructions, headers, or block "
        "labels other than the in-paragraph descriptors. Output only the enriched "
        "scene paragraph."
    ),
    "suffix.resolution": (
        "STRICT CORRECTION: the FINAL sentence MUST be verbatim: '1920x1080 at 30 "
        "fps, clean handheld framing, natural colour, in-camera audio at 48 kHz "
        "stereo.' (replace 'stereo' with 'mono' only if a single voice). Do not "
        "alter the resolution, fps, framing, colour, or audio language."
    ),
    "marketing_adjectives": (
        "STRICT CORRECTION: ban marketing adjectives: stunning, breathtaking, "
        "epic, mesmerising, cinematic, jaw-dropping, awe-inspiring, etc. Use plain "
        "concrete descriptors only."
    ),
    "forbidden_resolution": (
        "STRICT CORRECTION: never write 4K, 8K, UHD, or any resolution other than "
        "1920x1080. The mandatory suffix sets resolution."
    ),
    "specialised_hardware": (
        "STRICT CORRECTION: no aerial drone, Steadicam, IMAX, RED, Arri, ProRes, "
        "or other specialised cinema hardware. Plain handheld or static framing only."
    ),
    "decimal_timestamp": (
        "STRICT CORRECTION: never write decimal timestamps like t=3.5s. The clip "
        "is a single continuous take with no explicit time references."
    ),
}

GENERIC_HINT = (
    "STRICT CORRECTION: the previous attempt failed validation. Re-write the "
    "paragraph and follow every rule in the system prompt exactly. Keep one "
    "camera move, finish with the mandatory suffix sentence, and stay within the "
    "word band."
)


def build_hint(failures) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    rules = []
    for f in failures or []:
        rule = (f.get("rule") if isinstance(f, dict) else getattr(f, "rule", "")) or ""
        if rule:
            rules.append(rule.strip())
    for r in rules:
        hint = RULE_HINTS.get(r)
        if hint and hint not in seen:
            parts.append(hint)
            seen.add(hint)
    if not parts:
        parts.append(GENERIC_HINT)
    return "\n\n".join(parts)
