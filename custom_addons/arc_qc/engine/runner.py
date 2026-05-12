"""QC engine runner — orchestrates all validation phases for a session directory."""

from __future__ import annotations

import os
import time

from .types import Finding, GameInfo, GameResult, ModelDirInfo, QcConfig, QcResult, Severity
from .verdict import compute_verdict
from .phases import (
    content_safety,
    cross_run,
    discovery,
    runs_validation,
    smell_tests,
    steps_validation,
    structural,
)
from .schemas import CANONICAL_MODELS


def run_qc(session_path: str, config: QcConfig | None = None) -> QcResult:
    """Run the full QC pipeline on a session directory.

    Phases (matching the QC spec):
        0. Discovery — find games and model dirs
        1. Structural — file existence, encoding, JSONL parse
        2. Runs validation — 25 required fields, type/value checks, invariants
        3. Steps validation — 23 required fields, action allow-list, sequences
        4. Cross-run — runs ↔ steps consistency
        6. Content safety — ~40 regex patterns
        8. Smell tests — statistical anomalies

    Returns:
        QcResult with verdict, findings, and counters.
    """
    if config is None:
        config = QcConfig()

    t0 = time.monotonic()
    all_findings: list[Finding] = []

    # --- Phase 0: Discovery ---
    games, discovery_findings = discovery.discover_games(session_path)
    all_findings.extend(discovery_findings)

    # Early exit if no games or session dir missing
    if not games:
        verdict, crit, high, med, low = compute_verdict(all_findings)
        return QcResult(
            verdict=verdict,
            findings=all_findings,
            duration_seconds=time.monotonic() - t0,
            critical_count=crit,
            high_count=high,
            medium_count=med,
            low_count=low,
        )

    models_checked = 0
    runs_checked = 0
    steps_checked = 0
    game_results: list[GameResult] = []

    for game in games:
        game_findings: list[Finding] = []
        game_runs = 0
        game_steps = 0
        all_total_levels: list[int] = []

        # --- Phase 1: Structural ---
        structural_findings = structural.validate_files(game)
        game_findings.extend(structural_findings)

        # Determine if structural problems block deeper validation
        critical_files = {
            f.file_path
            for f in structural_findings
            if f.severity == Severity.CRITICAL and f.code in (
                'MISSING_FILE', 'EMPTY_FILE', 'NULL_BYTE',
                'INVALID_UTF8', 'JSONL_PARSE_ERROR',
            )
        }

        for mdir in game.model_dirs:
            runs_path = os.path.join(mdir.path, 'runs.jsonl')
            steps_path = os.path.join(mdir.path, 'steps.jsonl')

            if runs_path in critical_files or steps_path in critical_files:
                models_checked += 1
                continue

            runs_findings, parsed_runs = runs_validation.validate(mdir, config)
            game_findings.extend(runs_findings)
            game_runs += len(parsed_runs)

            for r in parsed_runs:
                tl = r.get('total_levels')
                if isinstance(tl, int):
                    all_total_levels.append(tl)

            steps_findings, parsed_steps = steps_validation.validate(mdir)
            game_findings.extend(steps_findings)
            game_steps += len(parsed_steps)

            cross_findings = cross_run.validate(
                mdir,
                runs_data=parsed_runs,
                steps_data=parsed_steps,
            )
            game_findings.extend(cross_findings)

            if not config.skip_content_safety and parsed_steps:
                safety_findings = content_safety.scan(
                    mdir_name=mdir.model_name,
                    game_id=mdir.game_id,
                    steps_data=parsed_steps,
                    fpath=steps_path,
                )
                game_findings.extend(safety_findings)

            if not config.skip_smell_tests and parsed_runs and parsed_steps:
                canonical = CANONICAL_MODELS.get(mdir.model_name, {})
                model_name = canonical.get('model', mdir.model_name)
                smell_findings = smell_tests.validate(
                    mdir_name=mdir.model_name,
                    game_id=mdir.game_id,
                    model_name=model_name,
                    runs_data=parsed_runs,
                    steps_data=parsed_steps,
                )
                game_findings.extend(smell_findings)

            models_checked += 1

        # §11.3: total_levels identical across all 12 runs per game
        if all_total_levels and len(set(all_total_levels)) > 1:
            game_findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='cross_run',
                code='TOTAL_LEVELS_INCONSISTENT',
                message=(
                    f'Game {game.game_id}: total_levels not identical across runs: '
                    f'{sorted(set(all_total_levels))}'
                ),
                file_path=game.path,
                field_name='total_levels',
                spec_ref='Section 11.3',
            ))

        # Stamp game_id on each finding for direct linkage
        for f in game_findings:
            f.game_id = game.game_id

        all_findings.extend(game_findings)
        runs_checked += game_runs
        steps_checked += game_steps

        game_verdict, g_crit, g_high, g_med, g_low = compute_verdict(game_findings)
        game_results.append(GameResult(
            game_id=game.game_id,
            game_path=game.path,
            verdict=game_verdict,
            models_found=len(game.model_dirs),
            models_expected=len(CANONICAL_MODELS),
            runs_checked=game_runs,
            steps_checked=game_steps,
            critical_count=g_crit,
            high_count=g_high,
            medium_count=g_med,
            low_count=g_low,
            findings=game_findings,
            model_dirs=[mdir for mdir in game.model_dirs],
        ))

    verdict, crit, high, med, low = compute_verdict(all_findings)

    return QcResult(
        verdict=verdict,
        findings=all_findings,
        game_results=game_results,
        games_checked=len(games),
        models_checked=models_checked,
        runs_checked=runs_checked,
        steps_checked=steps_checked,
        duration_seconds=time.monotonic() - t0,
        critical_count=crit,
        high_count=high,
        medium_count=med,
        low_count=low,
    )
