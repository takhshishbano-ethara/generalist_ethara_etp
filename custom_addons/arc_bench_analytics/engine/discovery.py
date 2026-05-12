"""Discovery phase — walk a session directory to find games and model subdirs."""

from __future__ import annotations

import os


def discover_games(session_path: str) -> list[dict]:
    """Discover game directories and their model subdirectories.

    Returns a list of dicts:
        [
            {
                'game_id': 'ch91',
                'game_path': '/abs/path/ch91',
                'models': [
                    {'model_dir': 'Claude_Opus_4.7', 'model_path': '/abs/path/ch91/Claude_Opus_4.7'},
                    ...
                ]
            },
            ...
        ]
    """
    if not os.path.isdir(session_path):
        raise FileNotFoundError(f'Session path does not exist: {session_path}')

    games = []
    # Skip known non-game entries
    skip_dirs = {'plot1_all', 'plot2_all', 'plot1_score_over_steps', 'plot2_score_vs_cost', '.git', '__pycache__', '.DS_Store'}
    skip_files = {
        'QC_REPORT.md', 'qc_findings.json', 'qc_validate.py', 'metadata.json',
    }

    for entry in sorted(os.listdir(session_path)):
        if entry in skip_dirs or entry.startswith('.'):
            continue
        entry_path = os.path.join(session_path, entry)
        if not os.path.isdir(entry_path):
            continue

        # A game directory must contain at least one model subdirectory
        # with runs.jsonl inside
        models = []
        for sub in sorted(os.listdir(entry_path)):
            sub_path = os.path.join(entry_path, sub)
            if not os.path.isdir(sub_path):
                continue
            if sub.startswith('.') or sub in ('traces', '__pycache__'):
                continue
            runs_file = os.path.join(sub_path, 'runs.jsonl')
            if os.path.isfile(runs_file):
                models.append({
                    'model_dir': sub,
                    'model_path': sub_path,
                })

        if models:
            games.append({
                'game_id': entry,
                'game_path': entry_path,
                'models': models,
            })

    return games
