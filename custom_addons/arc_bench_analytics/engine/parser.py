"""Parser — read and parse runs.jsonl and steps.jsonl files."""

from __future__ import annotations

import json
import os

from .types import RunData, StepData


def parse_runs(model_path: str, game_id: str) -> list[RunData]:
    """Parse runs.jsonl from a model directory.

    Each line is a JSON object representing one complete run.
    """
    runs_path = os.path.join(model_path, 'runs.jsonl')
    if not os.path.isfile(runs_path):
        return []

    runs = []
    with open(runs_path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            runs.append(RunData(
                run_id=obj.get('run_id', ''),
                model=obj.get('model', ''),
                game_id=game_id,
                run_number=obj.get('run_number', 0),
                total_steps=obj.get('total_steps', 0),
                max_steps=obj.get('max_steps', 200),
                final_score_pct=float(obj.get('final_score_pct', 0.0)),
                solved=bool(obj.get('solved', False)),
                levels_completed=obj.get('levels_completed', 0),
                total_levels=obj.get('total_levels', 0),
                cost_usd=float(obj.get('cost_usd', 0.0)),
                total_input_tokens=obj.get('total_input_tokens', 0),
                total_output_tokens=obj.get('total_output_tokens', 0),
                total_reasoning_tokens=obj.get('total_reasoning_tokens', 0),
                elapsed_seconds=float(obj.get('elapsed_seconds', 0.0)),
                error=obj.get('error'),
            ))
    return runs


def parse_steps(model_path: str, game_id: str) -> list[list[StepData]]:
    """Parse steps.jsonl from a model directory.

    Each line is a JSON array of step objects for one run.
    Returns a list of lists (one inner list per run).
    """
    steps_path = os.path.join(model_path, 'steps.jsonl')
    if not os.path.isfile(steps_path):
        return []

    all_runs_steps = []
    with open(steps_path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            arr = json.loads(line)
            run_steps = []
            for obj in arr:
                run_steps.append(StepData(
                    run_id=obj.get('run_id', ''),
                    model=obj.get('model', ''),
                    game_id=game_id,
                    run_number=obj.get('run_number', 0),
                    step=obj.get('step', 0),
                    score_pct=float(obj.get('score_pct', 0.0)),
                    cumulative_cost_usd=float(obj.get('cumulative_cost_usd', 0.0)),
                    level=obj.get('level', 0),
                    total_levels=obj.get('total_levels', 0),
                    done=bool(obj.get('done', False)),
                ))
            all_runs_steps.append(run_steps)

    return all_runs_steps
