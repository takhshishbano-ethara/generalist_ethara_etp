"""Schema definitions for ARC-AGI-3 trajectory QC validation.

All field lists, canonical values, and regex patterns are derived from
the GOLDEN_QC_IMPROVEMENT_ADDENDUM.md specification, adapted to match
the actual arc-explainer harness output format.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canonical model triples (directory name, model field, model_id field)
# ---------------------------------------------------------------------------

CANONICAL_MODELS: dict[str, dict[str, str]] = {
    'Claude_Opus_4.7': {
        'model': 'Claude Opus 4.7',
        'model_id': 'anthropic.claude-opus-4-7',
    },
    'Gemini_3.1_Pro': {
        'model': 'Gemini 3.1 Pro',
        'model_id': 'gemini-3.1-pro-preview',
    },
    'GPT_5.4_Thinking': {
        'model': 'GPT 5.4 Thinking',
        'model_id': 'gpt-5.4',
    },
    'Kimi_K2.5': {
        'model': 'Kimi K2.5',
        'model_id': 'moonshotai.kimi-k2.5',
    },
}

CANONICAL_MODEL_DIRS: list[str] = list(CANONICAL_MODELS.keys())
CANONICAL_MODEL_NAMES: list[str] = [v['model'] for v in CANONICAL_MODELS.values()]

# Mapping: model field value -> dir name (for reverse lookups)
MODEL_NAME_TO_DIR: dict[str, str] = {
    v['model']: k for k, v in CANONICAL_MODELS.items()
}

# Models that MUST have cached_input_tokens == 0
ZERO_CACHE_MODELS: set[str] = {'Claude Opus 4.7', 'Kimi K2.5'}

# Models exempt from reasoning_tokens == 0 smell test (§12.3)
REASONING_EXEMPT_MODELS: set[str] = {'Kimi K2.5', 'Claude Opus 4.7', 'Gemini 3.1 Pro'}

# ---------------------------------------------------------------------------
# Regexes (compiled for performance)
# ---------------------------------------------------------------------------

# run_id: "{Model Name}_{game_id}_run{N}"
# game_id may have optional suffix: bb01, bb01-v1, bb01-a1b2c3d4
RUN_ID_REGEX = re.compile(
    r'^(Claude Opus 4\.7|Gemini 3\.1 Pro|GPT 5\.4 Thinking|Kimi K2\.5)'
    r'_[a-z0-9]{2,10}_run[1-3]$'
)

# ISO-8601 UTC millisecond timestamp
TIMESTAMP_REGEX = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$'
)

# Action allow-list
# Grid is 64x64 (indices 0-63) but models sometimes output OOB coords
# which the harness accepts. We allow any non-negative integer coords.
ACTION_REGEX = re.compile(
    r'^(CLICK( \d+ \d+)?|LEFT|RIGHT|UP|DOWN|RESET|SELECT|UNDO)$'
)

# Observation header format
OBSERVATION_HEADER_REGEX = re.compile(
    r'^Grid \(64x64\) \| Level (\d+)/(\d+) \| Score: (\d+)% \| State: (NOT_FINISHED|WIN|GAME_OVER)$',
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------

RUNS_REQUIRED_FIELDS: set[str] = {
    'type', 'run_id', 'model', 'game_id', 'game_type', 'run_number',
    'total_steps', 'max_steps', 'final_score', 'solved',
    'levels_completed', 'total_levels', 'cost_usd',
    'total_input_tokens', 'total_output_tokens', 'total_reasoning_tokens',
    'elapsed_seconds', 'error', 'model_id',
    'final_score_pct', 'total_cached_input_tokens', 'total_cache_write_tokens',
    'reset_count', 'notepad_final', 'timestamp',
}

STEPS_REQUIRED_FIELDS: set[str] = {
    'run_id', 'run_number', 'model', 'game_id', 'step', 'action',
    'state', 'score', 'score_pct', 'level', 'total_levels',
    'reasoning', 'notepad_contents', 'done', 'timestamp',
    'observation', 'input_tokens', 'output_tokens', 'reasoning_tokens',
    'cached_input_tokens', 'step_cost_usd', 'cumulative_cost_usd',
}

# ---------------------------------------------------------------------------
# Valid values
# ---------------------------------------------------------------------------

VALID_STATES: set[str] = {'NOT_FINISHED', 'GAME_OVER', 'WIN'}
VALID_RUN_NUMBERS: set[int] = {1, 2, 3}

# ---------------------------------------------------------------------------
# Game ID validation
# ---------------------------------------------------------------------------

# game_id in JSONL may have a suffix (e.g. bb01-a1b2c3d4, bw01-v1)
# The directory name is the base (e.g. bb01, bw01)
# Validation: game_id must START with the directory name
GAME_ID_REGEX = re.compile(r'^[a-z0-9]{2,10}$')
