"""Phase 2 / Section 6: runs.jsonl validation — 25 required fields + 7 invariants."""

from __future__ import annotations

import json
import os

from ..types import Finding, ModelDirInfo, QcConfig, Severity
from ..schemas import (
    CANONICAL_MODELS,
    RUNS_REQUIRED_FIELDS,
    RUN_ID_REGEX,
    TIMESTAMP_REGEX,
    VALID_RUN_NUMBERS,
    ZERO_CACHE_MODELS,
)


def validate(mdir: ModelDirInfo, config: QcConfig | None = None) -> tuple[list[Finding], list[dict]]:
    """Validate runs.jsonl for a single model directory.

    Returns:
        (findings, parsed_runs) — parsed_runs may be used by downstream phases.
    """
    if config is None:
        config = QcConfig()
    findings: list[Finding] = []
    prefix = f'{mdir.game_id}/{mdir.model_name}'
    fpath = os.path.join(mdir.path, 'runs.jsonl')

    if not os.path.isfile(fpath):
        return findings, []  # Already flagged by structural phase

    # Load records
    records: list[dict] = []
    try:
        with open(fpath, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return findings, []  # Already flagged by structural phase

    # --- 6.1: Line count ---
    if len(records) != 3:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='runs',
            code='WRONG_RUN_COUNT',
            message=f'{prefix}/runs.jsonl: expected exactly 3 lines, got {len(records)}',
            file_path=fpath,
            expected='3',
            actual=str(len(records)),
            spec_ref='Section 6.1',
        ))

    # Get canonical info for this model dir
    canonical = CANONICAL_MODELS.get(mdir.model_name, {})
    expected_model = canonical.get('model', '')
    expected_model_id = canonical.get('model_id', '')

    seen_run_ids: set[str] = set()
    seen_run_numbers: set[int] = set()

    for line_num, record in enumerate(records, start=1):
        line_prefix = f'{prefix}/runs.jsonl:{line_num}'

        # --- 6.2: Required fields ---
        missing = RUNS_REQUIRED_FIELDS - set(record.keys())
        if missing:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='MISSING_FIELD',
                message=f'{line_prefix}: missing required fields: {sorted(missing)}',
                file_path=fpath,
                line_number=line_num,
                spec_ref='Section 6.2',
            ))

        # --- 6.3: Field-by-field contract ---
        # type
        if record.get('type') != 'run_complete':
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='INVALID_TYPE',
                message=f'{line_prefix}: type={record.get("type")!r}, expected "run_complete"',
                file_path=fpath,
                line_number=line_num,
                field_name='type',
                expected='run_complete',
                actual=str(record.get('type')),
                spec_ref='Section 6.3 / 6.4.6',
            ))

        # run_id
        run_id = record.get('run_id', '')
        if not RUN_ID_REGEX.match(str(run_id)):
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='INVALID_RUN_ID',
                message=f'{line_prefix}: run_id={run_id!r} does not match regex',
                file_path=fpath,
                line_number=line_num,
                field_name='run_id',
                spec_ref='Section 3.2',
            ))

        if run_id in seen_run_ids:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='DUPLICATE_RUN_ID',
                message=f'{line_prefix}: duplicate run_id={run_id!r}',
                file_path=fpath,
                line_number=line_num,
                field_name='run_id',
                spec_ref='Section 6.3',
            ))
        seen_run_ids.add(run_id)

        # model
        model = record.get('model', '')
        if expected_model and model != expected_model:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='MODEL_MISMATCH',
                message=f'{line_prefix}: model={model!r}, expected {expected_model!r}',
                file_path=fpath,
                line_number=line_num,
                field_name='model',
                expected=expected_model,
                actual=model,
                spec_ref='Section 3.1',
            ))

        game_id = record.get('game_id', '')
        if str(game_id) != mdir.game_id:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='GAME_ID_MISMATCH',
                message=f'{line_prefix}: game_id={game_id!r}, expected {mdir.game_id!r}',
                file_path=fpath,
                line_number=line_num,
                field_name='game_id',
                expected=mdir.game_id,
                actual=game_id,
                spec_ref='Section 6.3',
            ))

        # game_type
        game_type = record.get('game_type', '')
        if not game_type:
            findings.append(Finding(
                severity=Severity.MEDIUM,
                phase='runs',
                code='EMPTY_GAME_TYPE',
                message=f'{line_prefix}: game_type is empty',
                file_path=fpath,
                line_number=line_num,
                field_name='game_type',
                spec_ref='Section 6.3',
            ))

        # run_number
        run_number = record.get('run_number')
        if run_number not in VALID_RUN_NUMBERS:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='INVALID_RUN_NUMBER',
                message=f'{line_prefix}: run_number={run_number!r}, expected one of {VALID_RUN_NUMBERS}',
                file_path=fpath,
                line_number=line_num,
                field_name='run_number',
                spec_ref='Section 6.3',
            ))
        else:
            seen_run_numbers.add(run_number)

        # Validate run_id matches {model}_{game_id}_run{run_number}
        if expected_model and run_id and game_id:
            expected_run_id = f'{expected_model}_{game_id}_run{run_number}'
            if run_id != expected_run_id:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='runs',
                    code='RUN_ID_COMPOSITION_MISMATCH',
                    message=(
                        f'{line_prefix}: run_id={run_id!r} does not match '
                        f'expected {{model}}_{{game_id}}_run{{run_number}} = {expected_run_id!r}'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    field_name='run_id',
                    expected=expected_run_id,
                    actual=run_id,
                    spec_ref='Section 6.3',
                ))

        # total_steps, max_steps
        total_steps = record.get('total_steps')
        max_steps = record.get('max_steps')
        if isinstance(total_steps, int):
            if total_steps < 1:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='runs',
                    code='INVALID_TOTAL_STEPS',
                    message=f'{line_prefix}: total_steps={total_steps} < 1',
                    file_path=fpath,
                    line_number=line_num,
                    field_name='total_steps',
                    spec_ref='Section 6.3',
                ))
            if isinstance(max_steps, int) and total_steps > max_steps:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='runs',
                    code='TOTAL_EXCEEDS_MAX',
                    message=f'{line_prefix}: total_steps={total_steps} > max_steps={max_steps}',
                    file_path=fpath,
                    line_number=line_num,
                    field_name='total_steps',
                    spec_ref='Section 6.3',
                ))

        if max_steps != config.max_steps:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='INVALID_MAX_STEPS',
                message=f'{line_prefix}: max_steps={max_steps!r}, expected {config.max_steps}',
                file_path=fpath,
                line_number=line_num,
                field_name='max_steps',
                expected=str(config.max_steps),
                actual=str(max_steps),
                spec_ref='Section 6.3',
            ))

        # final_score
        final_score = record.get('final_score')
        if isinstance(final_score, (int, float)):
            if not (0.0 <= float(final_score) <= 1.0):
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='runs',
                    code='SCORE_OUT_OF_RANGE',
                    message=f'{line_prefix}: final_score={final_score} not in [0.0, 1.0]',
                    file_path=fpath,
                    line_number=line_num,
                    field_name='final_score',
                    spec_ref='Section 6.3',
                ))

        # solved
        solved = record.get('solved')
        if not isinstance(solved, bool):
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='SOLVED_NOT_BOOL',
                message=f'{line_prefix}: solved={solved!r} is not a boolean',
                file_path=fpath,
                line_number=line_num,
                field_name='solved',
                spec_ref='Section 6.3',
            ))

        # levels_completed, total_levels
        levels_completed = record.get('levels_completed')
        total_levels = record.get('total_levels')
        if isinstance(levels_completed, int) and isinstance(total_levels, int):
            if levels_completed < 0 or levels_completed > total_levels:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='runs',
                    code='LEVELS_OUT_OF_RANGE',
                    message=(
                        f'{line_prefix}: levels_completed={levels_completed} '
                        f'not in [0, total_levels={total_levels}]'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    field_name='levels_completed',
                    spec_ref='Section 6.3',
                ))

        if isinstance(total_levels, int) and total_levels < 1:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='INVALID_TOTAL_LEVELS',
                message=f'{line_prefix}: total_levels={total_levels} < 1',
                file_path=fpath,
                line_number=line_num,
                field_name='total_levels',
                spec_ref='Section 6.3',
            ))

        # cost_usd
        cost_usd = record.get('cost_usd')
        if isinstance(cost_usd, (int, float)) and float(cost_usd) <= 0:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='ZERO_COST',
                message=f'{line_prefix}: cost_usd={cost_usd} <= 0',
                file_path=fpath,
                line_number=line_num,
                field_name='cost_usd',
                spec_ref='Section 6.3',
            ))

        # Token fields (STANDARD severity)
        for token_field in ('total_input_tokens', 'total_output_tokens', 'total_reasoning_tokens'):
            val = record.get(token_field)
            if isinstance(val, int) and val < 0:
                findings.append(Finding(
                    severity=Severity.MEDIUM,
                    phase='runs',
                    code='NEGATIVE_TOKENS',
                    message=f'{line_prefix}: {token_field}={val} < 0',
                    file_path=fpath,
                    line_number=line_num,
                    field_name=token_field,
                    spec_ref='Section 6.3',
                ))

        # elapsed_seconds
        elapsed = record.get('elapsed_seconds')
        if isinstance(elapsed, (int, float)) and float(elapsed) <= 0:
            findings.append(Finding(
                severity=Severity.MEDIUM,
                phase='runs',
                code='ZERO_ELAPSED',
                message=f'{line_prefix}: elapsed_seconds={elapsed} <= 0',
                file_path=fpath,
                line_number=line_num,
                field_name='elapsed_seconds',
                spec_ref='Section 6.3',
            ))

        # error
        error = record.get('error')
        if error is not None and not isinstance(error, str):
            findings.append(Finding(
                severity=Severity.MEDIUM,
                phase='runs',
                code='INVALID_ERROR_TYPE',
                message=f'{line_prefix}: error field is {type(error).__name__}, expected string|null',
                file_path=fpath,
                line_number=line_num,
                field_name='error',
                spec_ref='Section 6.3',
            ))

        # model_id
        model_id = record.get('model_id', '')
        if expected_model_id and model_id != expected_model_id:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='MODEL_ID_MISMATCH',
                message=f'{line_prefix}: model_id={model_id!r}, expected {expected_model_id!r}',
                file_path=fpath,
                line_number=line_num,
                field_name='model_id',
                expected=expected_model_id,
                actual=model_id,
                spec_ref='Section 3.1',
            ))

        # final_score_pct
        final_score_pct = record.get('final_score_pct')
        if isinstance(final_score, (int, float)) and isinstance(final_score_pct, (int, float)):
            expected_pct = round(float(final_score) * 100)
            if abs(float(final_score_pct) - expected_pct) > 1e-6:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='runs',
                    code='SCORE_PCT_MISMATCH',
                    message=(
                        f'{line_prefix}: final_score_pct={final_score_pct}, '
                        f'expected round(final_score * 100)={expected_pct}'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    field_name='final_score_pct',
                    expected=str(expected_pct),
                    actual=str(final_score_pct),
                    spec_ref='Section 6.4.1',
                ))

        # total_cached_input_tokens (must be 0 for Claude/Kimi)
        cached = record.get('total_cached_input_tokens')
        if (
            isinstance(cached, int)
            and cached > 0
            and expected_model in ZERO_CACHE_MODELS
        ):
            findings.append(Finding(
                severity=Severity.MEDIUM,
                phase='runs',
                code='NONZERO_CACHED_TOKENS',
                message=(
                    f'{line_prefix}: total_cached_input_tokens={cached} > 0 '
                    f'for {expected_model} (must be 0)'
                ),
                file_path=fpath,
                line_number=line_num,
                field_name='total_cached_input_tokens',
                spec_ref='Section 6.3',
            ))

        # total_cache_write_tokens
        cwt = record.get('total_cache_write_tokens')
        if isinstance(cwt, int) and cwt < 0:
            findings.append(Finding(
                severity=Severity.MEDIUM,
                phase='runs',
                code='NEGATIVE_CACHE_WRITE',
                message=f'{line_prefix}: total_cache_write_tokens={cwt} < 0',
                file_path=fpath,
                line_number=line_num,
                field_name='total_cache_write_tokens',
                spec_ref='Section 6.3',
            ))

        # reset_count (validated in cross-run against steps)
        reset_count = record.get('reset_count')
        if isinstance(reset_count, int) and reset_count < 0:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='NEGATIVE_RESET_COUNT',
                message=f'{line_prefix}: reset_count={reset_count} < 0',
                file_path=fpath,
                line_number=line_num,
                field_name='reset_count',
                spec_ref='Section 6.3',
            ))

        # notepad_final
        notepad_final = record.get('notepad_final')
        if 'notepad_final' not in record:
            # Already caught by missing fields, but be explicit
            pass
        elif not isinstance(notepad_final, str):
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='NOTEPAD_FINAL_NOT_STRING',
                message=f'{line_prefix}: notepad_final is {type(notepad_final).__name__}, expected string',
                file_path=fpath,
                line_number=line_num,
                field_name='notepad_final',
                spec_ref='Section 6.3',
            ))

        # timestamp
        timestamp = record.get('timestamp')
        if timestamp is not None and isinstance(timestamp, str) and not TIMESTAMP_REGEX.match(timestamp):
            findings.append(Finding(
                severity=Severity.MEDIUM,
                phase='runs',
                code='INVALID_TIMESTAMP',
                message=f'{line_prefix}: timestamp={timestamp!r} does not match ISO-8601 regex',
                file_path=fpath,
                line_number=line_num,
                field_name='timestamp',
                spec_ref='Section 3.3',
            ))

        # --- Invariants ---
        _check_invariants(line_prefix, fpath, line_num, record, findings)

    # --- 6.3: run_number set must be exactly {1, 2, 3} ---
    if len(records) == 3 and seen_run_numbers != VALID_RUN_NUMBERS:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='runs',
            code='INCOMPLETE_RUN_NUMBERS',
            message=(
                f'{prefix}/runs.jsonl: run_number set is {sorted(seen_run_numbers)}, '
                f'expected {{1, 2, 3}}'
            ),
            file_path=fpath,
            expected='{1, 2, 3}',
            actual=str(sorted(seen_run_numbers)),
            spec_ref='Section 6.3',
        ))

    return findings, records


def _check_invariants(
    line_prefix: str, fpath: str, line_num: int, record: dict,
    findings: list[Finding],
) -> None:
    """Check invariants from section 6.4."""
    solved = record.get('solved')
    final_score = record.get('final_score')
    levels_completed = record.get('levels_completed')
    total_levels = record.get('total_levels')
    total_steps = record.get('total_steps')
    max_steps = record.get('max_steps')
    cost_usd = record.get('cost_usd')
    error = record.get('error')

    # 6.4.2: solved IFF final_score==1.0 AND levels_completed==total_levels
    if isinstance(solved, bool) and isinstance(final_score, (int, float)):
        fs = float(final_score)
        if solved:
            if abs(fs - 1.0) > 1e-6:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='runs',
                    code='SOLVED_SCORE_MISMATCH',
                    message=(
                        f'{line_prefix}: solved=true but final_score={final_score} != 1.0'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    spec_ref='Section 6.4.2',
                ))
            if isinstance(levels_completed, int) and isinstance(total_levels, int):
                if levels_completed != total_levels:
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        phase='runs',
                        code='SOLVED_LEVELS_MISMATCH',
                        message=(
                            f'{line_prefix}: solved=true but levels_completed='
                            f'{levels_completed} != total_levels={total_levels}'
                        ),
                        file_path=fpath,
                        line_number=line_num,
                        spec_ref='Section 6.4.2',
                    ))
        else:
            if abs(fs - 1.0) < 1e-6 and isinstance(levels_completed, int) and isinstance(total_levels, int):
                if levels_completed == total_levels:
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        phase='runs',
                        code='UNSOLVED_BUT_COMPLETE',
                        message=(
                            f'{line_prefix}: solved=false but final_score=1.0 and '
                            f'levels_completed==total_levels={total_levels}'
                        ),
                        file_path=fpath,
                        line_number=line_num,
                        spec_ref='Section 6.4.2',
                    ))

    # 6.4.3: unsolved + no error => total_steps == max_steps == 200
    if (
        isinstance(solved, bool) and not solved
        and error is None
        and isinstance(total_steps, int)
        and isinstance(max_steps, int)
    ):
        if total_steps != max_steps:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='UNSOLVED_SHORT_RUN',
                message=(
                    f'{line_prefix}: solved=false, error=null, but '
                    f'total_steps={total_steps} != max_steps={max_steps}'
                ),
                file_path=fpath,
                line_number=line_num,
                spec_ref='Section 6.4.3',
            ))

    # 6.4.4: total_steps >= 2 AND cost_usd > 0 (no dead-on-arrival)
    if isinstance(total_steps, int) and isinstance(cost_usd, (int, float)):
        if total_steps < 2 or float(cost_usd) <= 0:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='DEAD_ON_ARRIVAL',
                message=(
                    f'{line_prefix}: dead-on-arrival run — '
                    f'total_steps={total_steps}, cost_usd={cost_usd}'
                ),
                file_path=fpath,
                line_number=line_num,
                spec_ref='Section 6.4.4',
            ))

    # 6.4.5: reset_count checked in cross_run phase (needs steps data)

    # 11.7: final_score == levels_completed / total_levels
    if (
        isinstance(final_score, (int, float))
        and isinstance(levels_completed, int)
        and isinstance(total_levels, int)
        and total_levels > 0
    ):
        expected_score = levels_completed / total_levels
        if abs(float(final_score) - expected_score) > 1e-6:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='runs',
                code='SCORE_LEVEL_MISMATCH',
                message=(
                    f'{line_prefix}: final_score={final_score} != '
                    f'levels_completed/total_levels={levels_completed}/{total_levels}='
                    f'{expected_score:.6f}'
                ),
                file_path=fpath,
                line_number=line_num,
                spec_ref='Section 11.7',
            ))
