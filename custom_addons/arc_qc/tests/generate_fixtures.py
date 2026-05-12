"""Generate synthetic test fixtures for the QC engine.

Run this script directly:
    python -m custom_addons.arc_qc.tests.generate_fixtures

It creates two directories under tests/fixtures/:
    valid_game/   — passes all QC checks → verdict SHIP
    invalid_game/ — triggers specific errors → verdict BLOCK
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')

# Deterministic seed so fixtures are reproducible
random.seed(42)

MODELS = {
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

GAME_ID = 'ab12'
TOTAL_LEVELS = 4


def _ts(base: datetime, delta_seconds: float) -> str:
    dt = base + timedelta(seconds=delta_seconds)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def _make_steps(
    model_name: str, model_dir: str, game_id: str, run_number: int,
    num_steps: int, solved: bool, base_time: datetime,
) -> list[dict]:
    """Generate a list of step records for one run."""
    run_id = f'{model_name}_{game_id}_run{run_number}'
    steps = []
    cumulative_cost = 0.0

    for i in range(num_steps):
        is_last = (i == num_steps - 1)
        step_cost = 0.001 + (i * 0.0001)  # Slightly varying costs
        cumulative_cost += step_cost

        if solved and is_last:
            state = 'WIN'
            done = True
            score = 1.0
            level = TOTAL_LEVELS
            levels_completed = TOTAL_LEVELS
        elif not solved and is_last:
            state = 'NOT_FINISHED'
            done = False
            score = (TOTAL_LEVELS - 2) / TOTAL_LEVELS
            level = TOTAL_LEVELS - 1
            levels_completed = TOTAL_LEVELS - 2
        else:
            state = 'NOT_FINISHED'
            done = False
            level = min(1 + (i * TOTAL_LEVELS // num_steps), TOTAL_LEVELS)
            levels_completed = max(0, level - 1)
            score = levels_completed / TOTAL_LEVELS

        words = [
            'The grid shows', 'a pattern where', 'cells are arranged',
            'in clusters of', 'blue and red.', 'Moving right should',
            'advance the cursor', 'toward the target', 'configuration.',
            'After selecting', 'the highlighted cell,', 'I need to check',
            'whether the boundary', 'conditions match.', 'The score indicates',
            'partial progress—', 'level completion requires', 'all cells aligned.',
            'Resetting might help', 'if the current path', 'is suboptimal.',
        ]
        repeat = 1 + (i % 5)
        reasoning = f'Step {i}: ' + ' '.join(words * repeat)

        actions = ['UP', 'DOWN', 'LEFT', 'RIGHT', 'SELECT', 'RESET', 'UNDO',
                   'CLICK 10 20', 'CLICK 32 45']
        action = actions[i % len(actions)]

        observation_header = (
            f'Grid (64x64) | Level {level}/{TOTAL_LEVELS} | '
            f'Score: {round(score * 100)}% | State: {state}'
        )

        steps.append({
            'run_id': run_id,
            'run_number': run_number,
            'model': model_name,
            'game_id': game_id,
            'step': i,
            'action': action,
            'state': state,
            'score': score,
            'score_pct': round(score * 100),
            'level': level,
            'total_levels': TOTAL_LEVELS,
            'reasoning': reasoning,
            'notepad_contents': f'Step {i} notes for {model_name}',
            'done': done,
            'timestamp': _ts(base_time, i * 3.5 + random.uniform(0.1, 1.5) + run_number * 1000),
            'observation': observation_header + '\nGrid data would follow here.',
            'input_tokens': 1500 + i * 10,
            'output_tokens': 200 + i * 5,
            'reasoning_tokens': 0 if model_dir == 'Kimi_K2.5' else 100 + i * 3,
            'cached_input_tokens': 0,
            'step_cost_usd': round(step_cost, 6),
            'cumulative_cost_usd': round(cumulative_cost, 6),
        })

    return steps


def _make_run_record(
    model_name: str, model_id: str, model_dir: str, game_id: str,
    run_number: int, steps: list[dict],
) -> dict:
    """Generate a run_complete record from the steps."""
    last_step = steps[-1]
    total_cost = last_step['cumulative_cost_usd']
    solved = last_step['done'] and last_step['state'] == 'WIN'
    final_score = last_step['score']
    levels_completed = TOTAL_LEVELS if solved else max(
        0, last_step.get('level', 1) - 1
    )

    # For unsolved runs with no error, total_steps must equal max_steps=200
    total_steps = len(steps)
    reset_count = sum(1 for s in steps if s['action'] == 'RESET')

    return {
        'type': 'run_complete',
        'run_id': f'{model_name}_{game_id}_run{run_number}',
        'model': model_name,
        'game_id': game_id,
        'game_type': 'ARC-AGI-3',
        'run_number': run_number,
        'total_steps': total_steps,
        'max_steps': 200,
        'final_score': final_score,
        'solved': solved,
        'levels_completed': levels_completed,
        'total_levels': TOTAL_LEVELS,
        'cost_usd': round(total_cost, 6),
        'total_input_tokens': sum(s['input_tokens'] for s in steps),
        'total_output_tokens': sum(s['output_tokens'] for s in steps),
        'total_reasoning_tokens': sum(s['reasoning_tokens'] for s in steps),
        'elapsed_seconds': 3.5 * total_steps + 5.0,
        'error': None,
        'model_id': model_id,
        'final_score_pct': round(final_score * 100),
        'total_cached_input_tokens': 0,
        'total_cache_write_tokens': 0,
        'reset_count': reset_count,
        'notepad_final': steps[-1]['notepad_contents'],
        'timestamp': steps[-1]['timestamp'],
    }


def generate_valid_game():
    """Create a fully-valid game fixture that should yield SHIP."""
    game_dir = os.path.join(BASE_DIR, 'valid_game', GAME_ID)

    base_time = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)

    for model_dir, info in MODELS.items():
        mdir_path = os.path.join(game_dir, model_dir)
        os.makedirs(mdir_path, exist_ok=True)

        all_runs = []
        all_steps = []

        for run_number in (1, 2, 3):
            # For valid data: unsolved + no error => total_steps must be 200
            # Run 1: solved after 150 steps
            # Run 2: unsolved, 200 steps (exhausted)
            # Run 3: unsolved, 200 steps (exhausted)
            if run_number == 1:
                num_steps = 150
                solved = True
            else:
                num_steps = 200
                solved = False

            steps = _make_steps(
                info['model'], model_dir, GAME_ID, run_number,
                num_steps, solved, base_time,
            )
            run_rec = _make_run_record(
                info['model'], info['model_id'], model_dir, GAME_ID,
                run_number, steps,
            )
            all_runs.append(run_rec)
            all_steps.extend(steps)

        # Write JSONL files
        with open(os.path.join(mdir_path, 'runs.jsonl'), 'w') as f:
            for r in all_runs:
                f.write(json.dumps(r) + '\n')

        with open(os.path.join(mdir_path, 'steps.jsonl'), 'w') as f:
            for s in all_steps:
                f.write(json.dumps(s) + '\n')


def generate_invalid_game():
    """Create an invalid game fixture with specific errors.

    Errors injected:
    - Claude_Opus_4.7: wrong type field (not "run_complete")
    - Gemini_3.1_Pro: missing required field in runs.jsonl (model_id)
    - GPT_5.4_Thinking: steps.jsonl has wrong game_id
    - Kimi_K2.5: runs.jsonl has only 2 lines instead of 3
    """
    game_dir = os.path.join(BASE_DIR, 'invalid_game', GAME_ID)

    base_time = datetime(2026, 4, 28, 14, 0, 0, tzinfo=timezone.utc)

    for model_dir, info in MODELS.items():
        mdir_path = os.path.join(game_dir, model_dir)
        os.makedirs(mdir_path, exist_ok=True)

        all_runs = []
        all_steps = []

        runs_to_gen = (1, 2, 3)
        if model_dir == 'Kimi_K2.5':
            runs_to_gen = (1, 2)  # Only 2 runs — triggers WRONG_RUN_COUNT

        for run_number in runs_to_gen:
            if run_number == 1:
                num_steps = 150
                solved = True
            else:
                num_steps = 200
                solved = False

            steps = _make_steps(
                info['model'], model_dir, GAME_ID, run_number,
                num_steps, solved, base_time,
            )
            run_rec = _make_run_record(
                info['model'], info['model_id'], model_dir, GAME_ID,
                run_number, steps,
            )

            # Inject errors per model
            if model_dir == 'Claude_Opus_4.7' and run_number == 1:
                run_rec['type'] = 'run_partial'  # Wrong type

            if model_dir == 'Gemini_3.1_Pro' and run_number == 2:
                del run_rec['model_id']  # Missing required field

            if model_dir == 'GPT_5.4_Thinking':
                for s in steps:
                    if s['run_number'] == 1:
                        s['game_id'] = 'WRONG_ID'  # Wrong game_id

            all_runs.append(run_rec)
            all_steps.extend(steps)

        with open(os.path.join(mdir_path, 'runs.jsonl'), 'w') as f:
            for r in all_runs:
                f.write(json.dumps(r) + '\n')

        with open(os.path.join(mdir_path, 'steps.jsonl'), 'w') as f:
            for s in all_steps:
                f.write(json.dumps(s) + '\n')


if __name__ == '__main__':
    generate_valid_game()
    generate_invalid_game()
    print(f'Fixtures generated in {BASE_DIR}')
