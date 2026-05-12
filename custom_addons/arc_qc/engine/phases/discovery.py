"""Phase 0.1-0.2: Discovery — walk session directory, find games and model dirs."""

from __future__ import annotations

import os

from ..types import Finding, GameInfo, ModelDirInfo, Severity
from ..schemas import CANONICAL_MODEL_DIRS, GAME_ID_REGEX


_SKIP_DIRS = frozenset(('logs', '.git', '__pycache__', 'venv', 'node_modules'))


def discover_games(session_path: str) -> tuple[list[GameInfo], list[Finding]]:
    """Discover game directories within a session path.

    v2.1 scope boundary: Only subdirectories matching GAME_ID_REGEX are treated
    as game dirs. Everything else at batch root is ignored (out of scope).
    """
    findings: list[Finding] = []
    games: list[GameInfo] = []

    if not os.path.isdir(session_path):
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='discovery',
            code='SESSION_DIR_NOT_FOUND',
            message=f'Session directory does not exist or is not readable: {session_path}',
            spec_ref='Phase 0, check 0.1',
        ))
        return games, findings

    for entry in sorted(os.listdir(session_path)):
        entry_path = os.path.join(session_path, entry)
        if not os.path.isdir(entry_path):
            continue
        if entry in _SKIP_DIRS or entry.startswith('.'):
            continue

        if not GAME_ID_REGEX.match(entry):
            continue

        subdirs = [
            d for d in os.listdir(entry_path)
            if os.path.isdir(os.path.join(entry_path, d))
        ]
        if not subdirs:
            continue

        game = GameInfo(game_id=entry, path=entry_path)

        present_canonical = set()
        for subdir in sorted(subdirs):
            subdir_path = os.path.join(entry_path, subdir)

            if subdir in _SKIP_DIRS or subdir.startswith('.'):
                continue

            if subdir not in CANONICAL_MODEL_DIRS:
                continue

            has_runs = os.path.isfile(os.path.join(subdir_path, 'runs.jsonl'))
            has_steps = os.path.isfile(os.path.join(subdir_path, 'steps.jsonl'))

            present_canonical.add(subdir)

            model_dir = ModelDirInfo(
                model_name=subdir,
                path=subdir_path,
                game_id=entry,
                has_runs=has_runs,
                has_steps=has_steps,
            )
            game.model_dirs.append(model_dir)

        missing = set(CANONICAL_MODEL_DIRS) - present_canonical
        if missing:
            findings.append(Finding(
                severity=Severity.CRITICAL,
                phase='discovery',
                code='MISSING_MODEL_DIR',
                message=f'Game {entry}: missing canonical model directories: {sorted(missing)}',
                file_path=entry_path,
                expected=str(sorted(CANONICAL_MODEL_DIRS)),
                actual=str(sorted(present_canonical)),
                spec_ref='Phase 0, check 0.2',
            ))

        games.append(game)

    if not games:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='discovery',
            code='NO_GAMES_FOUND',
            message=f'No game directories found in session path: {session_path}',
            file_path=session_path,
            spec_ref='Phase 0, check 0.1',
        ))

    return games, findings
