"""Phase 4 / Section 7.6: Cross-run consistency checks."""

from __future__ import annotations

import json
import os

from ..types import Finding, ModelDirInfo, Severity


def validate(
    mdir: ModelDirInfo,
    runs_data: list[dict] | None = None,
    steps_data: list[dict] | None = None,
) -> list[Finding]:
    """Cross-validate runs.jsonl against steps.jsonl for a model directory.

    If runs_data / steps_data are provided, they are used directly.
    Otherwise, the files are read from disk.
    """
    findings: list[Finding] = []
    prefix = f'{mdir.game_id}/{mdir.model_name}'

    if runs_data is None:
        runs_data = _load_jsonl(os.path.join(mdir.path, 'runs.jsonl'))
    if steps_data is None:
        steps_data = _load_jsonl(os.path.join(mdir.path, 'steps.jsonl'))

    if runs_data is None or steps_data is None:
        return findings  # Files unreadable — already flagged by structural phase

    # --- run_id linkage ---
    run_ids_in_runs = {r.get('run_id') for r in runs_data if r.get('run_id')}
    run_ids_in_steps = {s.get('run_id') for s in steps_data if s.get('run_id')}

    # Orphan steps (run_id in steps but not in runs)
    orphan_step_ids = run_ids_in_steps - run_ids_in_runs
    if orphan_step_ids:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='cross_run',
            code='ORPHAN_STEP_RUN_ID',
            message=f'{prefix}: steps.jsonl contains run_ids not in runs.jsonl: {sorted(str(x) for x in orphan_step_ids if x)}',
            file_path=os.path.join(mdir.path, 'steps.jsonl'),
            spec_ref='Section 7.6',
        ))

    # Unrepresented runs (run_id in runs but not in steps)
    missing_step_ids = run_ids_in_runs - run_ids_in_steps
    if missing_step_ids:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='cross_run',
            code='UNREPRESENTED_RUN',
            message=f'{prefix}: runs.jsonl has run_ids with no steps: {sorted(str(x) for x in missing_step_ids if x)}',
            file_path=os.path.join(mdir.path, 'runs.jsonl'),
            spec_ref='Section 7.6',
        ))

    # --- Step count per run vs total_steps in runs.jsonl ---
    steps_by_run: dict[str, list[dict]] = {}
    for s in steps_data:
        rid = s.get('run_id', '')
        steps_by_run.setdefault(rid, []).append(s)

    for run in runs_data:
        rid = run.get('run_id', '')
        declared_total = run.get('total_steps')
        actual_count = len(steps_by_run.get(rid, []))

        if declared_total is not None and actual_count != declared_total:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='cross_run',
                code='STEP_COUNT_MISMATCH',
                message=(
                    f'{prefix}: run {rid} declares total_steps={declared_total} '
                    f'but steps.jsonl has {actual_count} steps'
                ),
                file_path=os.path.join(mdir.path, 'steps.jsonl'),
                expected=str(declared_total),
                actual=str(actual_count),
                spec_ref='Section 7.1',
            ))

    # --- Total line count ---
    expected_total = sum(r.get('total_steps', 0) for r in runs_data)
    actual_total = len(steps_data)
    if actual_total != expected_total:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='cross_run',
            code='TOTAL_STEP_COUNT_MISMATCH',
            message=(
                f'{prefix}: steps.jsonl has {actual_total} lines but '
                f'sum of total_steps across runs = {expected_total}'
            ),
            file_path=os.path.join(mdir.path, 'steps.jsonl'),
            expected=str(expected_total),
            actual=str(actual_total),
            spec_ref='Section 7.1',
        ))

    # --- game_id consistency ---
    for run in runs_data:
        gid = run.get('game_id', '')
        if gid != mdir.game_id:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='cross_run',
                code='GAME_ID_MISMATCH_RUN',
                message=f'{prefix}: runs.jsonl has game_id={gid!r}, expected {mdir.game_id!r}',
                file_path=os.path.join(mdir.path, 'runs.jsonl'),
                field_name='game_id',
                expected=mdir.game_id,
                actual=gid,
                spec_ref='Section 6.3',
            ))

    for idx, step in enumerate(steps_data):
        gid = step.get('game_id', '')
        if gid != mdir.game_id:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='cross_run',
                code='GAME_ID_MISMATCH_STEP',
                message=f'{prefix}: steps.jsonl:{idx + 1} has game_id={gid!r}, expected {mdir.game_id!r}',
                file_path=os.path.join(mdir.path, 'steps.jsonl'),
                line_number=idx + 1,
                field_name='game_id',
                expected=mdir.game_id,
                actual=gid,
                spec_ref='Section 7.3',
            ))
            break  # One finding is enough to flag

    # --- Last step terminal state vs run solved flag ---
    for run in runs_data:
        rid = run.get('run_id', '')
        solved = run.get('solved', False)
        run_steps = steps_by_run.get(rid, [])
        if not run_steps:
            continue

        last_step = max(run_steps, key=lambda s: s.get('step', -1))
        last_done = last_step.get('done', False)
        last_state = last_step.get('state', '')
        last_score = last_step.get('score', 0)

        if solved:
            if not last_done or last_state != 'WIN' or abs(last_score - 1.0) > 1e-6:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='cross_run',
                    code='TERMINAL_STATE_MISMATCH',
                    message=(
                        f'{prefix}: run {rid} solved=true but last step has '
                        f'done={last_done}, state={last_state!r}, score={last_score}'
                    ),
                    file_path=os.path.join(mdir.path, 'steps.jsonl'),
                    spec_ref='Section 7.6',
                ))
        else:
            if last_done and last_state == 'WIN':
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='cross_run',
                    code='TERMINAL_STATE_MISMATCH',
                    message=(
                        f'{prefix}: run {rid} solved=false but last step has '
                        f'done=true, state="WIN"'
                    ),
                    file_path=os.path.join(mdir.path, 'steps.jsonl'),
                    spec_ref='Section 7.6',
                ))

    # --- §6.4.5: reset_count == count(RESET actions) per run ---
    for run in runs_data:
        rid = run.get('run_id', '')
        declared_resets = run.get('reset_count')
        if declared_resets is None:
            continue
        run_steps = steps_by_run.get(rid, [])
        actual_resets = sum(1 for s in run_steps if s.get('action') == 'RESET')
        if declared_resets != actual_resets:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='cross_run',
                code='RESET_COUNT_MISMATCH',
                message=(
                    f'{prefix}: run {rid} declares reset_count={declared_resets} '
                    f'but steps have {actual_resets} RESET actions'
                ),
                file_path=os.path.join(mdir.path, 'runs.jsonl'),
                field_name='reset_count',
                expected=str(actual_resets),
                actual=str(declared_resets),
                spec_ref='Section 6.4.5',
            ))

    # --- §11.6: At every step with done=true, score==1.0 AND level==total_levels ---
    for step in steps_data:
        if step.get('done') is True:
            score = step.get('score', 0)
            level = step.get('level', 0)
            total_levels = step.get('total_levels', 0)
            if abs(score - 1.0) > 1e-6 or level != total_levels:
                step_idx = step.get('step', '?')
                rid = step.get('run_id', '?')
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='cross_run',
                    code='DONE_STATE_INVARIANT',
                    message=(
                        f'{prefix}: step {step_idx} (run {rid}) has done=true but '
                        f'score={score}, level={level}/{total_levels}'
                    ),
                    file_path=os.path.join(mdir.path, 'steps.jsonl'),
                    spec_ref='Section 11.6',
                ))
                break

    # --- §11.5: game_type consistent across runs/steps ---
    game_types_in_runs = {r.get('game_type', '') for r in runs_data}
    game_types_in_steps = {s.get('game_type', '') for s in steps_data if 'game_type' in s}
    all_game_types = game_types_in_runs | game_types_in_steps
    all_game_types.discard('')
    if len(all_game_types) > 1:
        findings.append(Finding(
            severity=Severity.MEDIUM,
            phase='cross_run',
            code='GAME_TYPE_INCONSISTENT',
            message=(
                f'{prefix}: inconsistent game_type values: {sorted(all_game_types)}'
            ),
            file_path=os.path.join(mdir.path, 'runs.jsonl'),
            field_name='game_type',
            spec_ref='Section 11.5',
        ))

    # H-4: Token sum verification (run totals >= sum of step tokens)
    for run in runs_data:
        rid = run.get('run_id', '')
        run_steps = steps_by_run.get(rid, [])
        if not run_steps:
            continue

        run_total_tokens = run.get('total_tokens')
        run_total_input = run.get('total_input_tokens')

        if isinstance(run_total_tokens, (int, float)) and run_steps:
            step_token_sum = sum(
                s.get('tokens', 0) for s in run_steps
                if isinstance(s.get('tokens'), (int, float))
            )
            if float(run_total_tokens) < step_token_sum - 1e-6:
                findings.append(Finding(
                    severity=Severity.HIGH,
                    phase='cross_run',
                    code='TOKEN_SUM_IMPOSSIBLE',
                    message=(
                        f'{prefix}: run {rid} total_tokens={run_total_tokens} < '
                        f'sum of step tokens={step_token_sum}'
                    ),
                    file_path=os.path.join(mdir.path, 'runs.jsonl'),
                    field_name='total_tokens',
                    spec_ref='Section 9.4',
                ))

        if isinstance(run_total_input, (int, float)) and run_steps:
            step_input_sum = sum(
                s.get('input_tokens', 0) for s in run_steps
                if isinstance(s.get('input_tokens'), (int, float))
            )
            if float(run_total_input) < step_input_sum - 1e-6:
                findings.append(Finding(
                    severity=Severity.HIGH,
                    phase='cross_run',
                    code='TOKEN_SUM_IMPOSSIBLE',
                    message=(
                        f'{prefix}: run {rid} total_input_tokens={run_total_input} < '
                        f'sum of step input_tokens={step_input_sum}'
                    ),
                    file_path=os.path.join(mdir.path, 'runs.jsonl'),
                    field_name='total_input_tokens',
                    spec_ref='Section 9.4',
                ))

    return findings


def _load_jsonl(fpath: str) -> list[dict] | None:
    """Load JSONL file, return list of dicts or None if unreadable."""
    if not os.path.isfile(fpath):
        return None
    try:
        records = []
        with open(fpath, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    parsed = json.loads(line)
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict):
                                records.append(item)
                    elif isinstance(parsed, dict):
                        records.append(parsed)
        return records
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
