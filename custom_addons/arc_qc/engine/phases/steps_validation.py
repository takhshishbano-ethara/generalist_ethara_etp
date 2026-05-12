"""Phase 3 / Section 7: steps.jsonl validation — 23 required fields, sequences, actions."""

from __future__ import annotations

import json
import os
from collections import defaultdict

from ..types import Finding, ModelDirInfo, Severity
from ..schemas import (
    CANONICAL_MODELS,
    STEPS_REQUIRED_FIELDS,
    ACTION_REGEX,
    OBSERVATION_HEADER_REGEX,
    RUN_ID_REGEX,
    TIMESTAMP_REGEX,
    VALID_STATES,
    VALID_RUN_NUMBERS,
    ZERO_CACHE_MODELS,
)


def validate(mdir: ModelDirInfo) -> tuple[list[Finding], list[dict]]:
    """Validate steps.jsonl for a single model directory.

    Returns:
        (findings, parsed_steps) — parsed_steps may be used by downstream phases.
    """
    findings: list[Finding] = []
    prefix = f'{mdir.game_id}/{mdir.model_name}'
    fpath = os.path.join(mdir.path, 'steps.jsonl')

    if not os.path.isfile(fpath):
        return findings, []  # Already flagged by structural phase

    # Load records
    records: list[dict] = []
    try:
        with open(fpath, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    parsed = json.loads(line)
                    # Handle both formats: one dict per line (standard JSONL)
                    # or one array-of-dicts per line (batched format)
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict):
                                records.append(item)
                    elif isinstance(parsed, dict):
                        records.append(parsed)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return findings, []  # Already flagged by structural phase

    canonical = CANONICAL_MODELS.get(mdir.model_name, {})
    expected_model = canonical.get('model', '')

    # Group by run_number for per-run checks
    steps_by_run: dict[int, list[tuple[int, dict]]] = defaultdict(list)

    for line_num, record in enumerate(records, start=1):
        line_prefix = f'{prefix}/steps.jsonl:{line_num}'

        # --- 7.2: Required fields ---
        missing = STEPS_REQUIRED_FIELDS - set(record.keys())
        if missing:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='MISSING_FIELD',
                message=f'{line_prefix}: missing required fields: {sorted(missing)}',
                file_path=fpath,
                line_number=line_num,
                spec_ref='Section 7.2',
            ))

        # --- 7.3: Field-by-field contract ---

        # run_id
        run_id = record.get('run_id', '')
        if isinstance(run_id, str) and run_id and not RUN_ID_REGEX.match(run_id):
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='INVALID_RUN_ID',
                message=f'{line_prefix}: run_id={run_id!r} does not match regex',
                file_path=fpath,
                line_number=line_num,
                field_name='run_id',
                spec_ref='Section 3.2',
            ))

        # run_number
        run_number = record.get('run_number')
        if run_number not in VALID_RUN_NUMBERS:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='INVALID_RUN_NUMBER',
                message=f'{line_prefix}: run_number={run_number!r}',
                file_path=fpath,
                line_number=line_num,
                field_name='run_number',
                spec_ref='Section 7.3',
            ))
        elif isinstance(run_number, int):
            steps_by_run[run_number].append((line_num, record))

        # model
        model = record.get('model', '')
        if expected_model and model != expected_model:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='MODEL_MISMATCH',
                message=f'{line_prefix}: model={model!r}, expected {expected_model!r}',
                file_path=fpath,
                line_number=line_num,
                field_name='model',
                spec_ref='Section 7.3',
            ))

        game_id = record.get('game_id', '')
        if str(game_id) != mdir.game_id:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='GAME_ID_MISMATCH',
                message=f'{line_prefix}: game_id={game_id!r}, expected {mdir.game_id!r}',
                file_path=fpath,
                line_number=line_num,
                field_name='game_id',
                spec_ref='Section 7.3',
            ))

        # action
        action = record.get('action', '')
        if isinstance(action, str) and not ACTION_REGEX.match(action):
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='INVALID_ACTION',
                message=f'{line_prefix}: action={action!r} not in allow-list',
                file_path=fpath,
                line_number=line_num,
                field_name='action',
                spec_ref='Section 3.4',
            ))
        # CLICK coordinate bounds check (§3.4: each coord ∈ [0, 63])
        if isinstance(action, str) and action.startswith('CLICK '):
            parts = action.split()
            if len(parts) == 3:
                try:
                    x, y = int(parts[1]), int(parts[2])
                    if not (0 <= x <= 63 and 0 <= y <= 63):
                        findings.append(Finding(
                            severity=Severity.CRITICAL,
                            phase='steps',
                            code='CLICK_OUT_OF_BOUNDS',
                            message=f'{line_prefix}: CLICK coordinates ({x}, {y}) out of [0, 63]',
                            file_path=fpath,
                            line_number=line_num,
                            field_name='action',
                            spec_ref='Section 3.4',
                        ))
                except ValueError:
                    pass  # Already caught by regex

        # state
        state = record.get('state', '')
        if state not in VALID_STATES:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='INVALID_STATE',
                message=f'{line_prefix}: state={state!r} not in {VALID_STATES}',
                file_path=fpath,
                line_number=line_num,
                field_name='state',
                spec_ref='Section 3.5',
            ))

        # score
        score = record.get('score')
        if isinstance(score, (int, float)):
            if not (0.0 <= float(score) <= 1.0):
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='SCORE_OUT_OF_RANGE',
                    message=f'{line_prefix}: score={score} not in [0.0, 1.0]',
                    file_path=fpath,
                    line_number=line_num,
                    field_name='score',
                    spec_ref='Section 7.3',
                ))

        # score_pct
        score_pct = record.get('score_pct')
        if isinstance(score, (int, float)) and isinstance(score_pct, (int, float)):
            expected_pct = round(float(score) * 100)
            if abs(float(score_pct) - expected_pct) > 1e-6:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='SCORE_PCT_MISMATCH',
                    message=(
                        f'{line_prefix}: score_pct={score_pct}, '
                        f'expected round(score * 100)={expected_pct}'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    field_name='score_pct',
                    spec_ref='Section 7.3',
                ))

        # level
        level = record.get('level')
        total_levels = record.get('total_levels')
        if isinstance(level, int):
            if level < 1:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='LEVEL_TOO_LOW',
                    message=f'{line_prefix}: level={level} < 1',
                    file_path=fpath,
                    line_number=line_num,
                    field_name='level',
                    spec_ref='Section 7.3',
                ))
            if isinstance(total_levels, int) and level > total_levels:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='LEVEL_EXCEEDS_TOTAL',
                    message=f'{line_prefix}: level={level} > total_levels={total_levels}',
                    file_path=fpath,
                    line_number=line_num,
                    field_name='level',
                    spec_ref='Section 7.3',
                ))

        # done <-> state consistency
        done = record.get('done')
        if isinstance(done, bool):
            if done and state != 'WIN':
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='DONE_STATE_MISMATCH',
                    message=f'{line_prefix}: done=true but state={state!r} (expected "WIN")',
                    file_path=fpath,
                    line_number=line_num,
                    field_name='done',
                    spec_ref='Section 7.3',
                ))
            if not done and state == 'WIN':
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='DONE_STATE_MISMATCH',
                    message=f'{line_prefix}: done=false but state="WIN"',
                    file_path=fpath,
                    line_number=line_num,
                    field_name='done',
                    spec_ref='Section 7.3',
                ))

        # timestamp
        timestamp = record.get('timestamp')
        if timestamp is not None and isinstance(timestamp, str) and not TIMESTAMP_REGEX.match(timestamp):
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='INVALID_TIMESTAMP',
                message=f'{line_prefix}: timestamp={timestamp!r} does not match ISO-8601 regex',
                file_path=fpath,
                line_number=line_num,
                field_name='timestamp',
                spec_ref='Section 3.3',
            ))

        # observation header
        observation = record.get('observation', '')
        if isinstance(observation, str) and observation:
            first_line = observation.split('\n', 1)[0]
            if not OBSERVATION_HEADER_REGEX.match(first_line):
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='INVALID_OBSERVATION_HEADER',
                    message=(
                        f'{line_prefix}: observation header does not match expected format. '
                        f'Got: {first_line!r}'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    field_name='observation',
                    spec_ref='Section 7.4',
                ))

        # Token fields (STANDARD severity)
        for token_field in ('input_tokens', 'output_tokens', 'reasoning_tokens'):
            val = record.get(token_field)
            if isinstance(val, int) and val < 0:
                findings.append(Finding(
                    severity=Severity.MEDIUM,
                    phase='steps',
                    code='NEGATIVE_TOKENS',
                    message=f'{line_prefix}: {token_field}={val} < 0',
                    file_path=fpath,
                    line_number=line_num,
                    field_name=token_field,
                    spec_ref='Section 7.3',
                ))

        # cached_input_tokens (must be 0 for Claude/Kimi)
        cached = record.get('cached_input_tokens')
        if (
            isinstance(cached, int)
            and cached > 0
            and expected_model in ZERO_CACHE_MODELS
        ):
            findings.append(Finding(
                severity=Severity.MEDIUM,
                phase='steps',
                code='NONZERO_CACHED_TOKENS',
                message=(
                    f'{line_prefix}: cached_input_tokens={cached} > 0 '
                    f'for {expected_model} (must be 0)'
                ),
                file_path=fpath,
                line_number=line_num,
                field_name='cached_input_tokens',
                spec_ref='Section 7.3',
            ))

        # step_cost_usd
        step_cost = record.get('step_cost_usd')
        if isinstance(step_cost, (int, float)) and float(step_cost) < 0:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='NEGATIVE_STEP_COST',
                message=f'{line_prefix}: step_cost_usd={step_cost} < 0',
                file_path=fpath,
                line_number=line_num,
                field_name='step_cost_usd',
                spec_ref='Section 9.2',
            ))

    # --- Per-run sequence checks (after grouping) ---
    for rn, indexed_steps in steps_by_run.items():
        _check_per_run_sequences(prefix, fpath, rn, indexed_steps, findings)

    return findings, records


def _check_per_run_sequences(
    prefix: str, fpath: str, run_number: int,
    indexed_steps: list[tuple[int, dict]], findings: list[Finding],
) -> None:
    """Check per-run sequence invariants: step contiguity, monotonicity, cost."""
    # Sort by step index within run
    sorted_steps = sorted(indexed_steps, key=lambda x: x[1].get('step', -1))

    # 7.5: step contiguous 0..N-1
    step_indices = [s[1].get('step') for s in sorted_steps]
    expected_indices = list(range(len(sorted_steps)))
    if step_indices != expected_indices:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='steps',
            code='STEP_SEQUENCE_GAP',
            message=(
                f'{prefix} run {run_number}: step indices are not contiguous 0..{len(sorted_steps) - 1}. '
                f'Got: {step_indices[:5]}...' if len(step_indices) > 5 else
                f'{prefix} run {run_number}: step indices are not contiguous. Got: {step_indices}'
            ),
            file_path=fpath,
            spec_ref='Section 7.5',
        ))

    # Timestamp monotonicity within run
    prev_ts = None
    for line_num, record in sorted_steps:
        ts = record.get('timestamp', '')
        if isinstance(ts, str) and ts:
            if prev_ts is not None and ts < prev_ts:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='TIMESTAMP_NOT_MONOTONIC',
                    message=(
                        f'{prefix}/steps.jsonl:{line_num} run {run_number}: '
                        f'timestamp {ts!r} < previous {prev_ts!r}'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    field_name='timestamp',
                    spec_ref='Section 7.3',
                ))
                break  # One finding per run is sufficient
            prev_ts = ts

    # cumulative_cost_usd monotonicity within run
    prev_cost = None
    for line_num, record in sorted_steps:
        cum_cost = record.get('cumulative_cost_usd')
        if isinstance(cum_cost, (int, float)):
            if prev_cost is not None and float(cum_cost) < float(prev_cost) - 1e-10:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='COST_NOT_MONOTONIC',
                    message=(
                        f'{prefix}/steps.jsonl:{line_num} run {run_number}: '
                        f'cumulative_cost_usd={cum_cost} < previous={prev_cost}'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    field_name='cumulative_cost_usd',
                    spec_ref='Section 9.3',
                ))
                break
            prev_cost = cum_cost

    # total_levels consistency within run
    total_levels_set = {s[1].get('total_levels') for s in sorted_steps}
    total_levels_set.discard(None)
    if len(total_levels_set) > 1:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='steps',
            code='TOTAL_LEVELS_INCONSISTENT',
            message=(
                f'{prefix} run {run_number}: total_levels varies within run: {total_levels_set}'
            ),
            file_path=fpath,
            spec_ref='Section 7.3',
        ))

    # --- Sequential invariant scan (C-1, C-2, H-1, H-2, H-5) ---
    _check_sequential_invariants(prefix, fpath, run_number, sorted_steps, findings)
    # --- RESET state consistency & score-level formula (7.4) ---
    _check_reset_and_score_level(prefix, fpath, run_number, sorted_steps, findings)


# Legal next-states from each state (validated against production data)
_LEGAL_TRANSITIONS: dict[str, set[str]] = {
    'NOT_FINISHED': {'NOT_FINISHED', 'WIN', 'GAME_OVER'},
    'WIN': set(),
    'GAME_OVER': {'GAME_OVER', 'NOT_FINISHED'},
}


def _check_sequential_invariants(
    prefix: str, fpath: str, run_number: int,
    sorted_steps: list[tuple[int, dict]], findings: list[Finding],
) -> None:
    """Walk steps in order, validating state transitions, score monotonicity,
    done-terminality, cost additivity, and level progression."""
    if len(sorted_steps) < 2:
        return

    done_seen_at: int | None = None

    for i in range(1, len(sorted_steps)):
        prev_line, prev_rec = sorted_steps[i - 1]
        curr_line, curr_rec = sorted_steps[i]

        prev_state = prev_rec.get('state', '')
        curr_state = curr_rec.get('state', '')
        curr_action = curr_rec.get('action', '')
        prev_action = prev_rec.get('action', '')

        # H-1: No steps after done=true
        if done_seen_at is None and prev_rec.get('done') is True:
            done_seen_at = prev_line
        if done_seen_at is not None:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='steps',
                code='STEPS_AFTER_DONE',
                message=(
                    f'{prefix}/steps.jsonl:{curr_line} run {run_number}: '
                    f'step exists after done=true (at line {done_seen_at})'
                ),
                file_path=fpath,
                line_number=curr_line,
                spec_ref='Section 7.6',
            ))
            break

        # C-1: State transition validation (action-aware)
        if prev_state in _LEGAL_TRANSITIONS and curr_state:
            # RESET from any state → NOT_FINISHED is always legal
            if curr_action == 'RESET' and curr_state == 'NOT_FINISHED':
                pass
            else:
                allowed = _LEGAL_TRANSITIONS[prev_state]
                if curr_state not in allowed:
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        phase='steps',
                        code='ILLEGAL_STATE_TRANSITION',
                        message=(
                            f'{prefix}/steps.jsonl:{curr_line} run {run_number}: '
                            f'illegal transition {prev_state}→{curr_state} (action={curr_action!r})'
                        ),
                        file_path=fpath,
                        line_number=curr_line,
                        spec_ref='Section 3.4',
                    ))
                    break

        prev_score = prev_rec.get('score')
        curr_score = curr_rec.get('score')
        if isinstance(prev_score, (int, float)) and isinstance(curr_score, (int, float)):
            if float(curr_score) < float(prev_score) - 1e-6:
                prev_level = prev_rec.get('level', 1)
                curr_level = curr_rec.get('level', 1)
                prev_prev_level = sorted_steps[i - 2][1].get('level', 1) if i >= 2 else prev_level
                just_arrived_at_new_level = (prev_level > prev_prev_level)
                score_drop_allowed = (
                    curr_action == 'RESET'
                    and curr_level == 1
                    and (prev_action == 'RESET' or just_arrived_at_new_level)
                ) or (
                    prev_state == 'GAME_OVER'
                    and float(curr_score) == 0
                )
                if not score_drop_allowed:
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        phase='steps',
                        code='SCORE_NOT_MONOTONIC',
                        message=(
                            f'{prefix}/steps.jsonl:{curr_line} run {run_number}: '
                            f'Score dropped {prev_score}->{curr_score} on non-RESET '
                            f'step {curr_rec.get("step")} '
                            f'(action={curr_action!r}, prev_action={prev_action!r})'
                        ),
                        file_path=fpath,
                        line_number=curr_line,
                        field_name='score',
                        spec_ref='Section 7.3',
                    ))

        # H-2: Cumulative cost additive consistency
        prev_cum = prev_rec.get('cumulative_cost_usd')
        curr_cum = curr_rec.get('cumulative_cost_usd')
        curr_step_cost = curr_rec.get('step_cost_usd')
        if (
            isinstance(prev_cum, (int, float))
            and isinstance(curr_cum, (int, float))
            and isinstance(curr_step_cost, (int, float))
        ):
            expected_min = float(prev_cum) + float(curr_step_cost) - 1e-6
            if float(curr_cum) < expected_min:
                findings.append(Finding(
                    severity=Severity.HIGH,
                    phase='steps',
                    code='COST_ADDITIVE_MISMATCH',
                    message=(
                        f'{prefix}/steps.jsonl:{curr_line} run {run_number}: '
                        f'cumulative_cost_usd={curr_cum} < prev({prev_cum}) + step_cost({curr_step_cost})'
                    ),
                    file_path=fpath,
                    line_number=curr_line,
                    field_name='cumulative_cost_usd',
                    spec_ref='Section 9.3',
                ))

        # H-5: Level progression — drops legal on RESET or post-RESET only
        prev_levels = prev_rec.get('levels_completed')
        curr_levels = curr_rec.get('levels_completed')
        total_levels = curr_rec.get('total_levels')
        if isinstance(prev_levels, int) and isinstance(curr_levels, int):
            curr_level = curr_rec.get('level', 1)
            prev_level_val = prev_rec.get('level', 1)
            prev_prev_level_val = sorted_steps[i - 2][1].get('level', 1) if i >= 2 else prev_level_val
            just_arrived = (prev_level_val > prev_prev_level_val)
            level_drop_allowed = (
                curr_action == 'RESET'
                and curr_level == 1
                and (prev_action == 'RESET' or just_arrived)
            ) or (
                prev_state == 'GAME_OVER'
            )
            if level_drop_allowed:
                pass
            else:
                delta = curr_levels - prev_levels
                if delta < 0 or delta > 1:
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        phase='steps',
                        code='LEVEL_PROGRESSION_VIOLATION',
                        message=(
                            f'{prefix}/steps.jsonl:{curr_line} run {run_number}: '
                            f'levels_completed changed by {delta} (expected 0 or +1): '
                            f'{prev_levels}→{curr_levels} (action={curr_action!r})'
                        ),
                        file_path=fpath,
                        line_number=curr_line,
                        field_name='levels_completed',
                        spec_ref='Section 9.1',
                    ))
            # Always: levels_completed <= total_levels
            if isinstance(total_levels, int) and curr_levels > total_levels:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='LEVELS_EXCEED_TOTAL',
                    message=(
                        f'{prefix}/steps.jsonl:{curr_line} run {run_number}: '
                        f'levels_completed={curr_levels} > total_levels={total_levels}'
                    ),
                    file_path=fpath,
                    line_number=curr_line,
                    field_name='levels_completed',
                    spec_ref='Section 9.1',
                ))


def _check_reset_and_score_level(
    prefix: str, fpath: str, run_number: int,
    sorted_steps: list[tuple[int, dict]], findings: list[Finding],
) -> None:
    """RESET_STATE_INCONSISTENT and SCORE_LEVEL_MISMATCH checks."""
    prev_action: str | None = None
    prev_step: dict | None = None
    prev_state: str | None = None

    for line_num, record in sorted_steps:
        action = record.get('action', '')
        score = record.get('score', 0)
        level = record.get('level', 1)
        total = record.get('total_levels', 1)

        if action == 'RESET' and isinstance(score, (int, float)) and isinstance(level, int):
            if float(score) == 0 and level > 1 and prev_state != 'GAME_OVER':
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='steps',
                    code='RESET_STATE_INCONSISTENT',
                    message=(
                        f'{prefix}/steps.jsonl:{line_num} run {run_number}: '
                        f'RESET at step {record.get("step")}: score=0 but level={level} '
                        f'(impossible: levels_completed=0 but on level>{1}), '
                        f'prev_level={prev_step.get("level") if prev_step else "N/A"}, '
                        f'prev_score={prev_step.get("score") if prev_step else "N/A"}'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    spec_ref='Section 7.4',
                ))

        if action != 'RESET' and prev_action != 'RESET':
            if (
                isinstance(score, (int, float))
                and isinstance(level, int)
                and isinstance(total, int)
                and total > 0
            ):
                expected = (level - 1) / total
                if abs(float(score) - expected) > 1e-6:
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        phase='steps',
                        code='SCORE_LEVEL_MISMATCH',
                        message=(
                            f'{prefix}/steps.jsonl:{line_num} run {run_number}: '
                            f'step {record.get("step")}: score={score} != '
                            f'(level-1)/total = ({level}-1)/{total} = {expected}'
                        ),
                        file_path=fpath,
                        line_number=line_num,
                        spec_ref='Section 7.4',
                    ))

        prev_action = action
        prev_step = record
        prev_state = record.get('state', '')
