from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys

_logger = logging.getLogger(__name__)

_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VALIDATOR_PATH = os.path.join(_MODULE_ROOT, "t2av_validator.py")
_VALIDATOR_MODULE_NAME = "_crowley_t2av_validator"

_validator_module = None


def _get_validator():
    global _validator_module
    if _validator_module is not None:
        return _validator_module
    spec = importlib.util.spec_from_file_location(
        _VALIDATOR_MODULE_NAME, _VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"Could not load t2av_validator from {_VALIDATOR_PATH}"
        )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_VALIDATOR_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    _validator_module = mod
    return mod


def validate(prompt: str, *, style=None, category=None):
    mod = _get_validator()
    style_norm = (style or "").strip().lower() or None
    category_norm = (category or "").strip().lower() or None
    return mod.validate(prompt, style=style_norm, category=category_norm)


def word_band_for_style(style):
    mod = _get_validator()
    style_norm = (style or "").strip().lower()
    band = mod.STYLE_WORD_BANDS.get(style_norm) or mod.GLOBAL_WORD_BAND
    lo, hi = int(band[0]), int(band[1])
    return lo, hi


def categorize(report) -> str:
    if report.fatal:
        return "fatal"
    if report.warnings:
        return "warned"
    return "clean"


def serialize_findings(findings) -> str:
    if not findings:
        return ""
    return json.dumps(
        [
            {
                "rule": f.rule,
                "severity": f.severity,
                "message": (f.message or "")[:500],
                "evidence": (f.evidence or "")[:500],
            }
            for f in findings
        ],
        ensure_ascii=False,
    )
