from __future__ import annotations

import logging
import re

_logger = logging.getLogger(__name__)

_LLM_SPECIAL_TOKENS = (
    "<|start|>", "<|end|>", "<|eom|>", "<|im_start|>", "<|im_end|>",
    "<|system|>", "<|user|>", "<|assistant|>", "<|return|>",
)

_CHAT_TEMPLATE_MARKERS = ("<s>", "</s>", "[INST]", "[/INST]", "<bos>", "<eos>")

_TO_SELF_RE = re.compile(r"^\s*to\s*=?\s*self", re.IGNORECASE)

_ASSISTANT_LOOP_RE = re.compile(r"(?:\bassistant\b\W*){4,}", re.IGNORECASE)

_CONCAT_REPEAT_RE = re.compile(r"\b([A-Za-z]{2,20})(?:\W*\1){4,}", re.IGNORECASE)

_META_LEAK_RE = re.compile(
    r"^\s*You are generating"
    r"|\bTARGET\s+SUB-?CATEGORY\b"
    r"|\bPROMPTING\s+STYLE\b"
    r"|\bSTYLE\s+EXAMPLES\b"
    r"|^\s*Rules:\s*$"
    r"|\bOutput\s+format\b",
    re.IGNORECASE | re.MULTILINE,
)

_MIN_CHARS = 30
_MIN_WORDS = 8
_RUNAWAY_CHARS = 2000
_BAD_CHAR_RATIO = 0.30
_TOPIC_TOKEN_MIN_LEN = 4
_TOPIC_TOKENS_FOR_STRICT_MISMATCH = 2
_LATIN_RATIO_THRESHOLD = 0.6
_MIN_ALPHA_FOR_LANG_CHECK = 20

_HIGH_CONFIDENCE_SIGNALS = frozenset({
    "empty",
    "llm_special_token",
    "chat_template_marker",
    "to_self_marker",
    "assistant_loop",
    "concat_repeat",
    "too_short",
    "runaway_length",
    "excessive_special_chars",
})

_LOW_CONFIDENCE_SIGNALS = frozenset({
    "meta_leak",
    "language_mismatch",
    "topic_irrelevance",
})


def detect_ambiguity(
    prompt: str,
    *,
    language: str | None = None,
    topic: str | None = None,
    category: str | None = None,
) -> dict:
    if not prompt or not prompt.strip():
        return {
            "is_ambiguous": True,
            "reasons": ["empty"],
            "high_confidence_signals": ["empty"],
            "low_confidence_signals": [],
            "confidence": 1.0,
        }

    p = prompt.strip()
    reasons: list[str] = []

    if any(tok in p for tok in _LLM_SPECIAL_TOKENS):
        reasons.append("llm_special_token")

    if any(marker in p for marker in _CHAT_TEMPLATE_MARKERS):
        reasons.append("chat_template_marker")

    if _TO_SELF_RE.match(p):
        reasons.append("to_self_marker")

    if _ASSISTANT_LOOP_RE.search(p):
        reasons.append("assistant_loop")

    if _CONCAT_REPEAT_RE.search(p):
        reasons.append("concat_repeat")

    if _META_LEAK_RE.search(p):
        reasons.append("meta_leak")

    char_len = len(p)
    word_len = len(p.split())
    if char_len < _MIN_CHARS or word_len < _MIN_WORDS:
        reasons.append("too_short")
    if char_len > _RUNAWAY_CHARS:
        reasons.append("runaway_length")

    bad_chars = sum(
        1 for c in p
        if not c.isprintable() or (0xE000 <= ord(c) <= 0xF8FF)
    )
    if char_len > 0 and (bad_chars / char_len) > _BAD_CHAR_RATIO:
        reasons.append("excessive_special_chars")

    if language and _has_language_mismatch(p, language):
        reasons.append("language_mismatch")

    if topic and _has_topic_irrelevance(p, topic):
        reasons.append("topic_irrelevance")

    high = [r for r in reasons if r in _HIGH_CONFIDENCE_SIGNALS]
    low = [r for r in reasons if r in _LOW_CONFIDENCE_SIGNALS]

    if high:
        confidence = min(1.0, 0.70 + 0.10 * len(high))
    elif low:
        confidence = min(0.60, 0.30 + 0.15 * len(low))
    else:
        confidence = 0.0

    return {
        "is_ambiguous": bool(reasons),
        "reasons": reasons,
        "high_confidence_signals": high,
        "low_confidence_signals": low,
        "confidence": confidence,
    }


def _has_language_mismatch(prompt: str, expected_language: str) -> bool:
    expected = (expected_language or "").strip().lower()
    if expected not in ("", "english", "en", "en-us", "en-gb"):
        return False
    total_alpha = sum(1 for c in prompt if c.isalpha())
    if total_alpha < _MIN_ALPHA_FOR_LANG_CHECK:
        return False
    latin_alpha = sum(1 for c in prompt if c.isascii() and c.isalpha())
    return (latin_alpha / total_alpha) < _LATIN_RATIO_THRESHOLD


def _has_topic_irrelevance(prompt: str, topic: str) -> bool:
    pattern = rf"\b[a-zA-Z]{{{_TOPIC_TOKEN_MIN_LEN},}}\b"
    topic_tokens = {t.lower() for t in re.findall(pattern, topic or "")}
    if len(topic_tokens) < _TOPIC_TOKENS_FOR_STRICT_MISMATCH:
        return False
    prompt_tokens = {t.lower() for t in re.findall(pattern, prompt or "")}
    return not (topic_tokens & prompt_tokens)
