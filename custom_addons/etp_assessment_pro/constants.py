# -*- coding: utf-8 -*-
"""Single source of truth for the assessment taxonomy (pure Python, no Odoo)."""

import json
import re

DEFAULT_SUBJECTIVE_THRESHOLD = 70.0

AB_VERDICT_WEIGHT = 0.75
AB_JUSTIFICATION_WEIGHT = 0.25

# SOP semantic-tag ranking (weighted Jaccard). A shared tag contributes its
# prefix weight; an unknown/blank prefix contributes TAG_DEFAULT_PREFIX_WEIGHT.
# Overridable per prefix via ir.config_parameter
# ``etp_assessment_pro.tag_weight_<prefix>`` (e.g. tag_weight_task).
TAG_PREFIX_WEIGHTS = {
    "task": 3.0,
    "domain": 2.0,
    "skill": 2.0,
    "modality": 1.0,
    "output-format": 1.0,
}
TAG_DEFAULT_PREFIX_WEIGHT = 1.0
# similar_count / action_view_similar gate: a generator counts as "similar" when
# the total weight of its SHARED tags with this one is >= this threshold (a
# weighted shared-weight, NOT the Jaccard ratio). Override via
# ir.config_parameter ``etp_assessment_pro.tag_similar_min_score``.
TAG_SIMILAR_MIN_SCORE_DEFAULT = 2.0

# Postgres advisory-lock keys. MUST stay mutually unique across the whole module
# (a duplicate key would make two independent locks alias and deadlock/skip).
# Keep every key registered here so a reviewer sees them side by side.
ADVISORY_LOCK_AUTOSCORE = 827193       # models/assessment.py    (session)
ADVISORY_LOCK_IMAGE_RENDER = 827194    # models/prompt.py        (session)
ADVISORY_LOCK_IMAGE_DETECT = 827195    # models/question_image.py(session)
ADVISORY_LOCK_VIDEO_POLL = 827196      # models/prompt.py        (session)
ADVISORY_LOCK_SKILL_EXTRACT = 827200   # models/prompt.py        (session)
ADVISORY_LOCK_QUESTION_GEN = 827201    # models/prompt.py        (session)
ADVISORY_LOCK_TAG_EXTRACT = 827202     # models/prompt.py        (session)
ADVISORY_LOCK_VERTEX_BEARER = 827300   # services/vertex.py      (xact)

QUESTION_TYPE_SELECTION = [
    ("mcq", "Objective - MCQ"),
    ("msq", "Objective - MSQ"),
    ("subjective_rubric", "Subjective - Rubric"),
    ("image_ab", "Image - A/B Evaluation"),
    ("image_prompt", "Image - Prompt"),
    ("image_label", "Image - Labelling"),
    ("video_prompt", "Video - Prompt"),
]
QUESTION_TYPE_CODES = frozenset(code for code, _label in QUESTION_TYPE_SELECTION)

OBJECTIVE_QUESTION_TYPES = frozenset({"mcq", "msq"})
SUBJECTIVE_QUESTION_TYPES = frozenset({"subjective_rubric"})
IMAGE_QUESTION_TYPES = frozenset({"image_ab", "image_prompt", "image_label"})
# video_prompt is the video twin of image_prompt: a reference clip + an output
# clip, candidate writes the transformation prompt, scored vs ideal_prompt.
# Phase 0 introduces only the taxonomy value and the parallel asset model; no
# generation/portal/scoring behaviour is wired yet.
VIDEO_QUESTION_TYPES = frozenset({"video_prompt"})

# image_label detection: photos -> object detection, screenshots -> UI elements.
DETECTION_MODE_SELECTION = [("object", "Objects"), ("ui", "UI Elements")]

QUESTION_TYPE_SHORT_LABELS = {
    "mcq": "MCQ",
    "msq": "MSQ",
    "subjective_rubric": "Rubric",
    "image_ab": "Image Comparison",
    "image_prompt": "Image Prompt",
    "image_label": "Image Labelling",
    "video_prompt": "Video Prompt",
}

QUESTION_TYPE_PROMPT_LIST = "/".join(code for code, _label in QUESTION_TYPE_SELECTION)


DIFFICULTY_SELECTION = [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")]
DIFFICULTY_CODES = frozenset(code for code, _label in DIFFICULTY_SELECTION)


MEDIUM_SELECTION = [("text", "Text"), ("image", "Image"), ("video", "Video")]
MEDIUM_CODES = frozenset(code for code, _label in MEDIUM_SELECTION)


AB_DIMENSIONS = [
    ("IF", "Instruction Following"),
    ("VQ", "Visual Quality"),
    ("LAI", "Less AI Generated"),
    ("OC", "Overall Choice"),
]
AB_DIMENSION_NAMES = {code: name for code, name in AB_DIMENSIONS}
AB_DIMENSION_CODES = frozenset(code for code, _name in AB_DIMENSIONS)

AB_CHOICES = ["Response A", "Response B", "Both Good", "Both Bad"]
AB_CHOICE_SET = frozenset(AB_CHOICES)


VERTEX_GLOBAL_LOCATION = "global"
VERTEX_DEFAULT_LOCATION = VERTEX_GLOBAL_LOCATION
VERTEX_DEFAULT_MODEL = "gemini-3-pro-image"
VERTEX_DEFAULT_IMAGE_MODEL = VERTEX_DEFAULT_MODEL
# Generation reads a SOP document, so it needs a document-capable model and must
# NOT fall back to the image model (gemini-3-pro-image), which sees a PDF as opaque
# binary -> "document has no pages". Override via etp_assessment_pro.generation_model.
GENERATION_DEFAULT_MODEL = "gemini-3.1-pro-preview"

# Veo async video generation (video_prompt Phase 3). OPTIONAL / config-gated:
# when no Veo model + Vertex bearer resolve, video_prompt clips stay upload-only
# (Phase 1). Veo is served ONLY on a regional endpoint (us-central1) — the
# gemini 'global' location 404s for Veo — so it gets its OWN location param.
VIDEO_DEFAULT_MODEL = "veo-3.1-generate-001"
VIDEO_DEFAULT_LOCATION = "us-central1"
VIDEO_DEFAULT_DURATION_S = 6


_OPTION_REASONING_RE = re.compile(
    r",\s+(because|since|due to|as it|making it|so that|therefore|"
    r"which makes|as the|given that)\b", re.IGNORECASE)


def option_name_reveals_reasoning(name):
    """True when an option label tacks a justification onto a short verdict — the
    'Image B, because it adheres...' leak. A lead-in longer than five words (e.g.
    a genuine MSQ statement) is left alone, so this never false-flags real claims."""
    if not name:
        return False
    match = _OPTION_REASONING_RE.search(name)
    return bool(match) and len(name[:match.start()].split()) <= 5


_SOURCE_REF_RE = re.compile(
    r"\bSOPs?\b"
    r"|\bstandard operating procedures?\b"
    r"|\b(?:Section|Sub-?section|Step|Clause|Rule)\s+\d"
    r"|\b(?:according to|as per|per|as (?:stated|specified|defined|described|"
    r"outlined|set out|laid out|mentioned) in|in accordance with|pursuant to|"
    r"as required by|in line with)\s+the\s+(?:\w+\s+){0,2}"
    r"(?:guidelines?|documentation|document|material|polic(?:y|ies)|"
    r"procedures?|manual|handbook|sop|protocol|spec(?:ification)?s?|"
    r"rubric|criteria|scheme|brief|playbook|runbook|standards?|"
    r"workflows?|process(?:es)?|framework)\b",
    re.IGNORECASE)


def text_has_source_reference(*texts):
    """True when any text cites the source material the candidate never sees
    ("according to the SOP", "Section 2.1", "per the guidelines"). Self-contained
    scenarios that merely mention "the prompt" or an in-scenario fact are not
    flagged."""
    for text in texts:
        if text and _SOURCE_REF_RE.search(text):
            return True
    return False


# --- Phase 1 scoring integrity: pre-LLM gates + post-LLM ceilings ------------
#
# INTEGRITY_GATE_PATTERNS drive services/gates.py. A candidate free-text answer
# matching ANY of these (case-insensitive, matched by services.gates via
# re.IGNORECASE) is treated as a prompt-injection attempt: it is resolved to a
# raw score of 0 locally and the Vertex grader is never called for it. The list
# is deliberately TIGHT — each entry targets a concrete "talk to / coerce the
# grader" idiom, not ordinary answer prose — so honest candidates who merely
# quote the prompt or hope for a good score are never gated. Everything inside a
# candidate answer is untrusted data, so these run before the LLM as a first
# line of defence complementary to the grader's own injection_attempt gate.
INTEGRITY_GATE_PATTERNS = [
    # "ignore the rubric" / "ignore this rubric"
    r"ignore\s+(?:the\s+|this\s+|all\s+)?rubric",
    # "ignore (all )?previous instructions" / "ignore prior instructions"
    r"ignore\s+(?:all\s+)?(?:previous\s+|prior\s+|the\s+above\s+)?instructions",
    # "disregard (the/all/any) (previous) instructions"
    r"disregard\s+(?:the\s+|all\s+|any\s+)?(?:previous\s+|prior\s+)?instructions",
    # "grading is waived" / "scoring is waived"
    r"(?:grading|scoring)\s+is\s+waived",
    # role-hijack: "you are now ..."
    r"you\s+are\s+now\b",
    # "award full/maximum marks|score|points|credit"
    r"award\s+(?:full|maximum|max)\s+(?:marks|score|points|credit)",
    # "output/return/give/set ... score ... 1.0|100|full|max"
    r"(?:output|print|return|give|assign|set)\b.{0,40}?\bscore\b.{0,20}?"
    r"(?:1\.0|100|full|max(?:imum)?)",
    # bare "score 100" / "score of 1.0" / "grade 100"
    r"(?:score|grade)\s+(?:of\s+)?(?:1\.0|100)\b",
]

# SCORE_CEILINGS drive services/scoring._apply_ceilings. Each ceiling is a hard
# 0-100 UPPER bound that can only LOWER the grader's raw score when its trigger
# signal is present in the v6 judge result; a missing signal simply skips that
# ceiling. These mirror the caps the grader is instructed to self-apply in
# prompts/scoring.md (verdict contradiction 0.25, two-or-more checklist-zero
# 0.55, two-or-more fabrications 0.25) and enforce them defensively at the
# platform boundary so an over-generous grader can never exceed them.
SCORE_CEILINGS = {
    "verdict_contradiction": 25.0,
    "multi_checklist_zero": 55.0,
    "fabrication": 25.0,
}


# --- image_ab flaw-injection (ground-truth-by-construction) ------------------
#
# For image_ab, generation plans a PAIR of images from a single target brief and
# stamps a per-dimension answer key DERIVED from the plan, so the key is
# trustworthy BY CONSTRUCTION rather than by model judgment. The plan is
# persisted on the draft AND the approved bank question as flaw_plan_json (the
# canonical 3-prompt shape; the legacy clean/flawed shape is still mapped in):
#   {"faithful_side": "a"|"b"|null/"both", "worker_prompt": ...,
#    "render_prompts": {"a": ..., "b": ...}, "planted": {"a":[...], "b":[...]},
#    "construction_keys": {"IF": ..., "VQ": ..., "LAI": ..., "OC": ...}}
#
# RELAXED per-dimension model (Phase 2): a planted flaw decides EXACTLY ONE
# dimension; dimensions no flaw touches resolve to "Both Good"; a side flawed on
# one dimension MAY still win a DIFFERENT dimension; when BOTH sides are flawed
# (faithful_side null/"both") an affected dimension may be "Both Bad". The one
# HARD guarantee kept is that Overall-Choice (OC) is ALWAYS decided to a single
# side (Response A/B, never Both Good/Both Bad) by a correctness-before-polish
# tiebreak. construction_keys maps each AB dimension CODE to its verdict. Two
# drift guards (approve + score time) hard-fail on any divergence between the
# materialized answer key and construction_keys. Every helper below no-ops on an
# empty/absent plan, so image_ab questions with NULL flaw_plan_json behave
# exactly as before.

AB_FLAWED_SIDES = ("a", "b")

_AB_CODE_PAREN_RE = re.compile(r"\(([A-Za-z]{1,4})\)")


def ab_side_verdict(side):
    """'a' -> 'Response A', anything else -> 'Response B'."""
    return "Response A" if (side or "").strip().lower() == "a" else "Response B"


def ab_other_side(side):
    """The opposite A/B slot: 'a' -> 'b', 'b' -> 'a'."""
    return "b" if (side or "").strip().lower() == "a" else "a"


def ab_code_from_label(label):
    """Map an AB dimension label back to its short code (IF/VQ/LAI/OC).

    Accepts the canonical 'Instruction Following (IF)', the bare code 'IF', or
    the full name 'Instruction Following'. Returns the uppercased code, or '' when
    the label matches no known AB dimension (so a non-AB axis is simply skipped
    by the drift guards)."""
    if not label:
        return ""
    m = _AB_CODE_PAREN_RE.search(label)
    if m:
        code = m.group(1).upper()
        if code in AB_DIMENSION_CODES:
            return code
    s = (label or "").strip()
    if s.upper() in AB_DIMENSION_CODES:
        return s.upper()
    for code, name in AB_DIMENSIONS:
        if name.strip().lower() == s.lower():
            return code
    return ""


def ab_dimension_label(code):
    """Canonical dimension label for a code: 'Instruction Following (IF)'."""
    code = (code or "").upper()
    name = AB_DIMENSION_NAMES.get(code)
    if name and code:
        return "%s (%s)" % (name, code)
    return name or code or ""


def ab_dimension_options(code):
    """Allowed options for an AB dimension. OC is a straight A/B choice; every
    other dimension additionally allows Both Good / Both Bad."""
    if (code or "").upper() == "OC":
        return ["Response A", "Response B"]
    return list(AB_CHOICES)


def ab_flip_verdict(verdict):
    """Swap the A/B side of a verdict; Both Good / Both Bad / Tie are unchanged.
    Used when the flawed image is randomly re-assigned to the opposite slot so
    the correct answer is not always 'Response A' (defeats position bias)."""
    v = (verdict or "").strip()
    if v == "Response A":
        return "Response B"
    if v == "Response B":
        return "Response A"
    return v


def ab_flip_construction_keys(keys):
    """Flip every A/B verdict in a construction_keys map (codes upper-cased)."""
    return {str(c).upper(): ab_flip_verdict(v) for c, v in (keys or {}).items()}


def normalize_flaw_plan(plan):
    """Map an OLD- or NEW-shape flaw_plan to the canonical NEW shape, or None.

    NEW (3-prompt): worker_prompt (the target shown to the candidate) is DISTINCT
    from render_prompts.a/.b (a full standalone brief per image); planted lists
    the flaws per side; faithful_side names the true side. OLD (still accepted):
    clean_prompt doubled as worker_prompt AND the faithful render prompt,
    flawed_prompt was the flawed render prompt, flawed_side named the flawed slot
    and injected_flaws were its planted flaws. Always returns both faithful_side
    and flawed_side (derived from each other); None when plan is not a dict."""
    if not isinstance(plan, dict):
        return None
    keys = {str(c).upper(): v
            for c, v in (plan.get("construction_keys") or {}).items()}
    is_new = bool(plan.get("render_prompts") or plan.get("worker_prompt")
                  or plan.get("faithful_side"))
    if is_new:
        rp = plan.get("render_prompts") or {}
        faithful = str(plan.get("faithful_side") or "").strip().lower()
        flawed = (ab_other_side(faithful)
                  if faithful in AB_FLAWED_SIDES else "")
        planted = plan.get("planted") or {}
        return {
            "faithful_side": faithful,
            "flawed_side": flawed,
            "worker_prompt": str(plan.get("worker_prompt") or "").strip(),
            "render_prompts": {
                "a": str(rp.get("a") or "").strip(),
                "b": str(rp.get("b") or "").strip(),
            },
            "planted": {
                "a": [str(f) for f in (planted.get("a") or []) if str(f).strip()],
                "b": [str(f) for f in (planted.get("b") or []) if str(f).strip()],
            },
            "construction_keys": keys,
        }
    flawed = str(plan.get("flawed_side") or "").strip().lower()
    faithful = ab_other_side(flawed) if flawed in AB_FLAWED_SIDES else ""
    clean = str(plan.get("clean_prompt") or "").strip()
    flawed_prompt = str(plan.get("flawed_prompt") or "").strip()
    injected = [str(f) for f in (plan.get("injected_flaws") or [])
                if str(f).strip()]
    render = {"a": "", "b": ""}
    planted = {"a": [], "b": []}
    if flawed in AB_FLAWED_SIDES:
        render[flawed] = flawed_prompt
        render[faithful] = clean
        planted[flawed] = injected
    return {
        "faithful_side": faithful,
        "flawed_side": flawed,
        "worker_prompt": clean,
        "render_prompts": render,
        "planted": planted,
        "construction_keys": keys,
    }


def validate_flaw_plan(plan):
    """Return the contract violations of a flaw-injection plan (empty == valid).

    Accepts BOTH the NEW 3-prompt shape and the OLD clean/flawed shape (via
    normalize_flaw_plan) and enforces the RELAXED per-dimension invariant. What is
    enforced here is only what is MECHANICALLY CHECKABLE from the plan structure:

      * faithful_side is "a", "b", or null/"both" (both-flawed);
      * a non-empty worker/target prompt and BOTH per-side render prompts;
      * a NON-EMPTY planted-flaw list on every FLAWED side (the one side opposite
        faithful_side, or BOTH sides when faithful_side is null/"both");
      * construction_keys cover ALL of IF/VQ/LAI/OC with per-dimension allowed
        verdicts (IF/VQ/LAI may be any of Response A/B/Both Good/Both Bad);
      * OC is DECIDED — Response A or Response B only, never Both Good/Both Bad
        (the single hard tiebreak guarantee we keep);
      * a "Both Bad" verdict on IF/VQ/LAI requires BOTH sides to be flawed
        (faithful_side null/"both"): a clean side cannot make a dimension bad.

    The SEMANTIC mapping — which planted flaw decides which dimension, whether a
    "Both Good" dimension is truly untouched by any flaw, and whether a side that
    NAMES a dimension genuinely deserves to win it — is NOT decidable from the
    free-text planted lists and is DEFERRED to the model plus the Phase-3
    verification loop. Consequently a flawed side MAY now legitimately name (win)
    a DIFFERENT dimension; the old "no verdict names the flawed side" and "OC
    names the clean side" rules are intentionally GONE. A plan failing the checks
    above is skipped/rejected by the caller."""
    if not isinstance(plan, dict):
        return ["flaw_plan is not an object"]
    norm = normalize_flaw_plan(plan)
    errs = []
    faithful = norm["faithful_side"]
    both_flawed = faithful not in AB_FLAWED_SIDES
    if not norm["worker_prompt"]:
        errs.append("worker_prompt (target prompt) required")
    if not norm["render_prompts"].get("a"):
        errs.append("render_prompts.a required")
    if not norm["render_prompts"].get("b"):
        errs.append("render_prompts.b required")
    if both_flawed:
        for slot in AB_FLAWED_SIDES:
            if not norm["planted"].get(slot):
                errs.append("planted flaws required on side %r (both-flawed plan)"
                            % slot)
    else:
        flawed = norm["flawed_side"]
        if flawed not in AB_FLAWED_SIDES:
            errs.append("flawed side must resolve to the 'a'/'b' slot opposite "
                        "the faithful side")
        elif not norm["planted"].get(flawed):
            errs.append("planted flaws must be a non-empty list on the flawed side")
    keys = norm["construction_keys"]
    if not keys:
        errs.append("construction_keys missing/empty")
        return errs
    missing = AB_DIMENSION_CODES - set(keys)
    if missing:
        errs.append("construction_keys missing codes %s" % sorted(missing))
    for code, _name in AB_DIMENSIONS:
        if code not in keys:
            continue
        verdict = keys[code]
        allowed = ab_dimension_options(code)
        if code == "OC" and verdict not in ("Response A", "Response B"):
            errs.append("construction_keys[OC]=%r must be DECIDED "
                        "(Response A or Response B)" % (verdict,))
            continue
        if verdict not in allowed:
            errs.append("construction_keys[%s]=%r not in %s"
                        % (code, verdict, allowed))
            continue
        if verdict == "Both Bad" and not both_flawed:
            errs.append("construction_keys[%s]='Both Bad' requires BOTH sides "
                        "flawed (faithful_side null/'both')" % (code,))
    return errs


def ab_specs_from_construction_keys(keys):
    """Build [{label, options, correct}] dimension specs for the four AB codes
    FROM construction_keys, with canonical labels and OC restricted to A/B. This
    is the deterministic derivation of the answer key from the plan."""
    norm = {str(c).upper(): v for c, v in (keys or {}).items()}
    specs = []
    for code, _name in AB_DIMENSIONS:
        verdict = norm.get(code)
        specs.append({
            "label": ab_dimension_label(code),
            "options": ab_dimension_options(code),
            "correct": [verdict] if verdict else [],
        })
    return specs


def parse_flaw_plan(flaw_plan_json):
    """Parse a flaw_plan_json string to a dict, or {} when absent/unparseable.
    Every drift guard no-ops on {} so pre-Phase-3 (NULL) questions are
    unaffected."""
    raw = (flaw_plan_json or "").strip()
    if not raw:
        return {}
    try:
        plan = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return plan if isinstance(plan, dict) else {}


def ab_construction_keys(flaw_plan_json):
    """The construction_keys map (codes upper-cased) from a flaw_plan_json
    string, or {} when there is no plan."""
    plan = parse_flaw_plan(flaw_plan_json)
    keys = plan.get("construction_keys") if isinstance(plan, dict) else None
    if not isinstance(keys, dict):
        return {}
    return {str(c).upper(): v for c, v in keys.items()}


def ab_key_drift(materialized_keys, construction_keys):
    """Return human-readable drift descriptions where the stored correct verdict
    for an AB code does not equal its construction key. Empty == no drift.
    ``materialized_keys`` maps CODE -> list of is_correct option names."""
    drift = []
    norm = {str(c).upper(): v for c, v in (construction_keys or {}).items()}
    for code, _name in AB_DIMENSIONS:
        expected = str(norm.get(code) or "").strip()
        got = [str(x).strip() for x in (materialized_keys.get(code) or [])]
        if not expected:
            drift.append("%s: no construction key" % code)
            continue
        if got != [expected]:
            drift.append("%s expected=%r stored=%r" % (code, expected, got))
    return drift
