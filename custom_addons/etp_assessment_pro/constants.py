# -*- coding: utf-8 -*-
"""Single source of truth for the assessment taxonomy (pure Python, no Odoo)."""

import re

# --------------------------------------------------------------------------- #
# Question types
# --------------------------------------------------------------------------- #
# Order IS the canonical display order used by every Selection field and view.
QUESTION_TYPE_SELECTION = [
    ("mcq", "Objective - MCQ"),
    ("msq", "Objective - MSQ"),
    ("subjective_justification", "Subjective - Justification"),
    ("subjective_rubric", "Subjective - Rubric"),
    ("image_ab", "Image - A/B Evaluation"),
    ("image_text", "Image - Prompt/Labelling"),
]
QUESTION_TYPE_CODES = frozenset(code for code, _label in QUESTION_TYPE_SELECTION)

# Type groupings used across scoring / portal / validation.
OBJECTIVE_QUESTION_TYPES = frozenset({"mcq", "msq"})
SUBJECTIVE_QUESTION_TYPES = frozenset(
    {"subjective_justification", "subjective_rubric"}
)
IMAGE_QUESTION_TYPES = frozenset({"image_ab", "image_text"})

# Short chip/title labels, distinct from the long Selection labels above.
QUESTION_TYPE_SHORT_LABELS = {
    "mcq": "MCQ",
    "msq": "MSQ",
    "subjective_justification": "Justification",
    "subjective_rubric": "Rubric",
    "image_ab": "Image Comparison",
    "image_text": "Image + Text",
}

# "/"-joined code list embedded verbatim in the generator prompts.
QUESTION_TYPE_PROMPT_LIST = "/".join(code for code, _label in QUESTION_TYPE_SELECTION)


# --------------------------------------------------------------------------- #
# Difficulty
# --------------------------------------------------------------------------- #
DIFFICULTY_SELECTION = [("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")]
DIFFICULTY_CODES = frozenset(code for code, _label in DIFFICULTY_SELECTION)


# --------------------------------------------------------------------------- #
# Source medium
# --------------------------------------------------------------------------- #
MEDIUM_SELECTION = [("text", "Text"), ("image", "Image")]
MEDIUM_CODES = frozenset(code for code, _label in MEDIUM_SELECTION)


# --------------------------------------------------------------------------- #
# Image A/B evaluation dimensions
# --------------------------------------------------------------------------- #
# Axes the generator emits for an image_ab question (short code -> label).
# Not a scoring contract: scoring reads whatever axes a question carries, by id.
AB_DIMENSIONS = [
    ("IF", "Instruction Following"),
    ("VQ", "Visual Quality"),
    ("LAI", "Less AI Generated"),
    ("OC", "Overall Choice"),
]
AB_DIMENSION_NAMES = {code: name for code, name in AB_DIMENSIONS}
AB_DIMENSION_CODES = frozenset(code for code, _name in AB_DIMENSIONS)

# Fallback A/B verdicts (no "Tie"); used only when a skill defines no own
# dimensions — otherwise generation reads axes/options from dimension records.
AB_CHOICES = ["Response A", "Response B", "Both Good", "Both Bad"]
AB_CHOICE_SET = frozenset(AB_CHOICES)


# --------------------------------------------------------------------------- #
# Vertex AI / Gemini defaults
# --------------------------------------------------------------------------- #
# Fallback defaults only; runtime reads the ``etp_assessment_pro.vertex_*``
# config params. ``global`` is also the endpoint sentinel: Gemini-3 (text+image)
# serves on the global endpoint, a non-global region routes to <region>-*.
VERTEX_GLOBAL_LOCATION = "global"
VERTEX_DEFAULT_LOCATION = VERTEX_GLOBAL_LOCATION
# ONE model for every task — extraction, generation, scoring AND image
# rendering. gemini-3-pro-image gives the best image quality and is a reasoning
# model: it splits output across parts and marks thinking with thought=True, so
# _call_vertex concatenates the NON-thought parts and _extract_json_array unwraps
# a wrapped {"skills": [...]} object (see services/vertex.py). Its calls are slow
# (heavy thinking), so the slow paths run OFF the web request — extraction and
# generation via cron drainers, scoring via _cron_llm_auto_score — to avoid the
# managed-Postgres idle-in-transaction connection reaper ('cursor already closed').
VERTEX_DEFAULT_MODEL = "gemini-3-pro-image"
VERTEX_DEFAULT_IMAGE_MODEL = VERTEX_DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# Answer-leak guard for objective options
# --------------------------------------------------------------------------- #
# An objective option's name is shown to the candidate verbatim, so a rationale
# baked into it ("Image B, because it adheres...") reveals the answer. Generation
# is told to keep options to the bare verdict (see prompts/question.md); this
# flags the leak pattern for reviewers WITHOUT mutating any text.
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


# --------------------------------------------------------------------------- #
# Source-leak guard: items must be self-contained
# --------------------------------------------------------------------------- #
# The candidate never sees the SOP / source docs, so an item (or its answer key)
# that cites them is unanswerable and leaks the project internals. The generator
# is told to bake principles into the scenario (see prompts/question.md); this
# flags the citation pattern for reviewers WITHOUT mutating any text. Referring
# to "the prompt" or an in-scenario fact is fine and is NOT matched.
_SOURCE_REF_RE = re.compile(
    r"\bSOPs?\b"
    r"|\bstandard operating procedures?\b"
    r"|\b(?:Section|Sub-?section|Step|Clause|Rule)\s+\d"
    r"|\b(?:according to|as per|per|as (?:stated|specified|defined|described|"
    r"outlined|set out|laid out|mentioned) in|in accordance with|pursuant to|"
    r"as required by|in line with)\s+the\s+(?:\w+\s+){0,2}"
    r"(?:guidelines?|documentation|document|material|polic(?:y|ies)|"
    r"procedures?|manual|handbook|sop|protocol|spec(?:ification)?s?|"
    r"rubric|criteria|scheme|brief|playbook|runbook|standards?)\b",
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
