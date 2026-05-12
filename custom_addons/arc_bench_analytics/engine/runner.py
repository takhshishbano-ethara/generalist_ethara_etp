"""Runner — orchestrates discovery, parsing, and subprocess plot generation."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from .aggregator import aggregate_model
from .discovery import discover_games
from .parser import parse_runs, parse_steps
from .types import AnalysisConfig, AnalysisResult, GameAnalysis


_DEFAULT_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'scripts', 'plot_results.py',
)


def _run_plot_script(
    script_path: str,
    session_path: str,
    timeout: int = 600,
) -> tuple[str, str, int]:
    """Execute the plotting script via subprocess.

    Returns (stdout, stderr, returncode).
    """
    cmd = [
        sys.executable,
        script_path,
        '--game', 'all',
        '--data-dir', session_path,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=session_path,
    )
    return result.stdout, result.stderr, result.returncode


def _collect_plot_png(game_path: str, filename: str) -> bytes:
    """Read a PNG file from disk, return empty bytes if not found."""
    path = os.path.join(game_path, filename)
    if os.path.isfile(path):
        with open(path, 'rb') as f:
            return f.read()
    return b''


def run_analysis(
    session_path: str,
    config: AnalysisConfig | None = None,
    script_path: str | None = None,
) -> AnalysisResult:
    """Run the full analysis pipeline on a session directory.

    Phases:
        1. Discovery — find games and model directories
        2. Parse — read runs.jsonl for aggregated metrics
        3. Plot — invoke external script via subprocess
        4. Collect — read generated PNGs from disk

    Args:
        session_path: Absolute path to the session directory.
        config: Optional analysis configuration.
        script_path: Path to the plotting script. Uses bundled default if None.

    Returns:
        AnalysisResult with all game analyses and generated plots.
    """
    if config is None:
        config = AnalysisConfig()

    if not script_path:
        script_path = _DEFAULT_SCRIPT

    if not os.path.isfile(script_path):
        return AnalysisResult(
            error=f'Plot script not found: {script_path}',
            duration_seconds=0.0,
        )

    t0 = time.monotonic()
    games_data = discover_games(session_path)

    if not games_data:
        return AnalysisResult(
            error='No game directories found in session path.',
            duration_seconds=time.monotonic() - t0,
        )

    # Run the plot script (generates all PNGs on disk)
    stdout, stderr, returncode = _run_plot_script(script_path, session_path)

    if returncode != 0:
        error_msg = stderr.strip() or stdout.strip() or f'Script exited with code {returncode}'
        return AnalysisResult(
            error=f'Plot script failed: {error_msg}',
            duration_seconds=time.monotonic() - t0,
            script_stdout=stdout,
            script_stderr=stderr,
        )

    # Parse data and collect plots for each game
    all_games: list[GameAnalysis] = []
    total_models = 0

    for game_info in games_data:
        game_id = game_info['game_id']
        game_path = game_info['game_path']

        model_analyses = []
        for model_info in game_info['models']:
            model_dir = model_info['model_dir']
            model_path = model_info['model_path']

            runs = parse_runs(model_path, game_id)
            steps_by_run = parse_steps(model_path, game_id)
            analysis = aggregate_model(
                model_dir=model_dir,
                runs=runs,
                steps_by_run=steps_by_run,
                max_steps=config.max_steps,
            )
            model_analyses.append(analysis)

        total_models += len(model_analyses)

        plot1_png = _collect_plot_png(game_path, 'plot1_score_over_steps.png')
        plot2_png = _collect_plot_png(game_path, 'plot2_score_vs_cost.png')

        game_analysis = GameAnalysis(
            game_id=game_id,
            game_path=game_path,
            models=model_analyses,
            plot1_png=plot1_png,
            plot2_png=plot2_png,
        )
        all_games.append(game_analysis)

    return AnalysisResult(
        games=all_games,
        game_count=len(all_games),
        model_count=total_models,
        duration_seconds=time.monotonic() - t0,
        script_stdout=stdout,
        script_stderr=stderr,
    )
