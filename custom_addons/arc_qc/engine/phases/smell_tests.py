"""Phase 8 / Section 12: Smell tests (ADVISORY unless noted)."""

from __future__ import annotations

import statistics
from datetime import datetime

from ..types import Finding, Severity
from ..schemas import REASONING_EXEMPT_MODELS


def validate(
    mdir_name: str,
    game_id: str,
    model_name: str,
    runs_data: list[dict],
    steps_data: list[dict],
) -> list[Finding]:
    """Run smell tests for one model directory.

    Args:
        mdir_name: e.g. "Claude_Opus_4.7"
        game_id: e.g. "ab12"
        model_name: canonical model name e.g. "Claude Opus 4.7"
        runs_data: parsed runs.jsonl records
        steps_data: parsed steps.jsonl records
    """
    findings: list[Finding] = []
    prefix = f'{game_id}/{mdir_name}'

    # Group steps by run_number
    steps_by_run: dict[int, list[dict]] = {}
    for s in steps_data:
        rn = s.get('run_number')
        if isinstance(rn, int):
            steps_by_run.setdefault(rn, []).append(s)

    for run_number, run_steps in steps_by_run.items():
        _check_reasoning_distribution(prefix, run_number, run_steps, findings)
        _check_empty_reasoning(prefix, run_number, run_steps, findings)
        _check_zero_tokens(prefix, run_number, model_name, run_steps, findings)
        _check_synthetic_timestamps(prefix, run_number, run_steps, findings)
        _check_linear_cost_slope(prefix, run_number, run_steps, findings)
        _check_empty_field_context_dump(prefix, run_number, run_steps, findings)

    _check_empty_notepad_final(prefix, runs_data, findings)

    return findings


def _check_reasoning_distribution(
    prefix: str, run_number: int, steps: list[dict], findings: list[Finding],
) -> None:
    """12.1: Cookie-cutter CoT — coefficient of variation < 0.1."""
    lengths = []
    for s in steps:
        r = s.get('reasoning', '')
        if isinstance(r, str) and r:
            lengths.append(len(r))

    if len(lengths) < 10:
        return  # Too few samples

    mean = statistics.mean(lengths)
    if mean == 0:
        return
    stdev = statistics.stdev(lengths)
    cv = stdev / mean

    if cv < 0.1:
        findings.append(Finding(
            severity=Severity.LOW,
            phase='smell_tests',
            code='COOKIE_CUTTER_COT',
            message=(
                f'{prefix} run {run_number}: reasoning length CV={cv:.4f} < 0.1 '
                f'(mean={mean:.0f}, stdev={stdev:.0f}) — suspiciously uniform'
            ),
            spec_ref='Section 12.1',
        ))


def _check_empty_reasoning(
    prefix: str, run_number: int, steps: list[dict], findings: list[Finding],
) -> None:
    """12.2: Empty reasoning > 30% of steps."""
    if not steps:
        return
    empty = sum(1 for s in steps if not s.get('reasoning'))
    ratio = empty / len(steps)
    if ratio > 0.3:
        findings.append(Finding(
            severity=Severity.LOW,
            phase='smell_tests',
            code='EMPTY_REASONING',
            message=(
                f'{prefix} run {run_number}: {empty}/{len(steps)} steps '
                f'({ratio:.0%}) have empty reasoning'
            ),
            spec_ref='Section 12.2',
        ))


def _check_zero_tokens(
    prefix: str, run_number: int, model_name: str, steps: list[dict],
    findings: list[Finding],
) -> None:
    """12.3: Zero output_tokens or reasoning_tokens > 10%."""
    if not steps:
        return

    zero_output = sum(1 for s in steps if s.get('output_tokens', 0) == 0)
    if zero_output / len(steps) > 0.1:
        findings.append(Finding(
            severity=Severity.LOW,
            phase='smell_tests',
            code='ZERO_OUTPUT_TOKENS',
            message=(
                f'{prefix} run {run_number}: {zero_output}/{len(steps)} steps '
                f'have output_tokens=0'
            ),
            spec_ref='Section 12.3',
        ))

    if model_name not in REASONING_EXEMPT_MODELS:
        zero_reasoning = sum(1 for s in steps if s.get('reasoning_tokens', 0) == 0)
        if zero_reasoning / len(steps) > 0.1:
            findings.append(Finding(
                severity=Severity.LOW,
                phase='smell_tests',
                code='ZERO_REASONING_TOKENS',
                message=(
                    f'{prefix} run {run_number}: {zero_reasoning}/{len(steps)} steps '
                    f'have reasoning_tokens=0'
                ),
                spec_ref='Section 12.3',
            ))


def _check_synthetic_timestamps(
    prefix: str, run_number: int, steps: list[dict], findings: list[Finding],
) -> None:
    """12.4: Synthetic timestamps (all same second or perfectly evenly spaced)."""

    timestamps = []
    for s in steps:
        ts = s.get('timestamp', '')
        if not isinstance(ts, str):
            continue
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            timestamps.append(dt.timestamp())
        except (ValueError, TypeError):
            continue

    if len(timestamps) < 3:
        return

    # All on same second?
    if max(timestamps) - min(timestamps) < 1.0:
        findings.append(Finding(
            severity=Severity.MEDIUM,
            phase='smell_tests',
            code='SYNTHETIC_TIMESTAMPS',
            message=(
                f'{prefix} run {run_number}: all {len(timestamps)} timestamps '
                f'fall within 1 second — likely synthetic'
            ),
            spec_ref='Section 12.4',
        ))
        return

    # Perfectly evenly spaced?
    deltas = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    if not deltas:
        return
    mean_delta = statistics.mean(deltas)
    if mean_delta == 0:
        return
    max_deviation = max(abs(d - mean_delta) for d in deltas)
    if max_deviation < 1e-8 * mean_delta:
        findings.append(Finding(
            severity=Severity.MEDIUM,
            phase='smell_tests',
            code='SYNTHETIC_TIMESTAMPS',
            message=(
                f'{prefix} run {run_number}: timestamps are perfectly evenly spaced '
                f'(delta={mean_delta:.6f}s) — likely synthetic'
            ),
            spec_ref='Section 12.4',
        ))


def _check_linear_cost_slope(
    prefix: str, run_number: int, steps: list[dict], findings: list[Finding],
) -> None:
    """12.5: Perfectly linear cumulative_cost_usd slope."""
    costs = []
    for s in steps:
        c = s.get('step_cost_usd')
        if isinstance(c, (int, float)):
            costs.append(float(c))

    if len(costs) < 10:
        return

    if all(abs(c - costs[0]) < 1e-8 for c in costs):
        findings.append(Finding(
            severity=Severity.LOW,
            phase='smell_tests',
            code='LINEAR_COST_SLOPE',
            message=(
                f'{prefix} run {run_number}: all {len(costs)} step_cost_usd values '
                f'are identical ({costs[0]:.8f}) — suspiciously uniform'
            ),
            spec_ref='Section 12.5',
        ))


def _check_empty_field_context_dump(
    prefix: str, run_number: int, steps: list[dict], findings: list[Finding],
) -> None:
    """§12.6: Report empty notepad_contents/reasoning per step."""
    empty_notepad_steps: list[int] = []
    empty_reasoning_steps: list[int] = []

    for s in steps:
        step_idx = s.get('step', -1)
        nc = s.get('notepad_contents', '')
        reasoning = s.get('reasoning', '')

        if isinstance(nc, str) and nc.strip() == '':
            empty_notepad_steps.append(step_idx)
        if isinstance(reasoning, str) and reasoning.strip() == '':
            empty_reasoning_steps.append(step_idx)

    total = len(steps)
    if total == 0:
        return

    if len(empty_notepad_steps) == total:
        findings.append(Finding(
            severity=Severity.MEDIUM,
            phase='smell_tests',
            code='EMPTY_FIELD_CONTEXT_DUMP',
            message=(
                f'{prefix} run {run_number}: notepad_contents empty on ALL '
                f'{total} steps'
            ),
            spec_ref='Section 12.6',
        ))

    if len(empty_reasoning_steps) == total:
        findings.append(Finding(
            severity=Severity.MEDIUM,
            phase='smell_tests',
            code='EMPTY_FIELD_CONTEXT_DUMP',
            message=(
                f'{prefix} run {run_number}: reasoning empty on ALL '
                f'{total} steps'
            ),
            spec_ref='Section 12.6',
        ))


def _check_empty_notepad_final(
    prefix: str, runs_data: list[dict], findings: list[Finding],
) -> None:
    """§12.6: Report empty notepad_final at run level."""
    for run in runs_data:
        nf = run.get('notepad_final', '')
        rn = run.get('run_number', '?')
        if isinstance(nf, str) and nf.strip() == '':
            findings.append(Finding(
                severity=Severity.MEDIUM,
                phase='smell_tests',
                code='EMPTY_FIELD_CONTEXT_DUMP',
                message=f'{prefix} run {rn}: notepad_final is empty',
                spec_ref='Section 12.6',
            ))
