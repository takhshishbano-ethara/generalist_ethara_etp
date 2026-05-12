"""Phase 0.3-0.6: Structural — file existence, JSONL parse, encoding checks."""

from __future__ import annotations

import json
import os

from ..types import Finding, GameInfo, ModelDirInfo, Severity


def validate_files(game: GameInfo) -> list[Finding]:
    """Validate file existence and basic structural integrity for all model dirs."""
    findings: list[Finding] = []

    for mdir in game.model_dirs:
        # Check 0.3: runs.jsonl and steps.jsonl exist and are non-empty
        for fname in ('runs.jsonl', 'steps.jsonl'):
            fpath = os.path.join(mdir.path, fname)
            if not os.path.isfile(fpath):
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='structural',
                    code='MISSING_FILE',
                    message=f'{mdir.game_id}/{mdir.model_name}/{fname} does not exist',
                    file_path=fpath,
                    spec_ref='Phase 0, check 0.3',
                ))
                continue

            size = os.path.getsize(fpath)
            if size == 0:
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='structural',
                    code='EMPTY_FILE',
                    message=f'{mdir.game_id}/{mdir.model_name}/{fname} is empty (0 bytes)',
                    file_path=fpath,
                    spec_ref='Phase 0, check 0.3',
                ))
                continue

            # Check 0.5: null bytes and replacement char
            _check_binary_safety(fpath, mdir, fname, findings)

            # Check 0.6: UTF-8 without BOM
            _check_encoding(fpath, mdir, fname, findings)

            # Check 0.4: JSONL parseable
            _check_jsonl_parse(fpath, mdir, fname, findings)

        # Check 0.3 note: unexpected files
        expected_files = {'runs.jsonl', 'steps.jsonl'}
        actual_files = set()
        if os.path.isdir(mdir.path):
            actual_files = {
                f for f in os.listdir(mdir.path)
                if os.path.isfile(os.path.join(mdir.path, f))
            }
        extras = actual_files - expected_files
        if extras:
            findings.append(Finding(
                severity=Severity.MEDIUM,
                phase='structural',
                code='UNEXPECTED_FILE',
                message=(
                    f'{mdir.game_id}/{mdir.model_name}: '
                    f'unexpected files: {sorted(extras)}'
                ),
                file_path=mdir.path,
                spec_ref='Phase 0, check 0.8',
            ))

    return findings


def _check_binary_safety(
    fpath: str, mdir: ModelDirInfo, fname: str, findings: list[Finding],
) -> None:
    """Check 0.5: no null bytes or Unicode replacement chars."""
    try:
        with open(fpath, 'rb') as f:
            content = f.read()
    except OSError as exc:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='structural',
            code='FILE_READ_ERROR',
            message=f'{mdir.game_id}/{mdir.model_name}/{fname}: cannot read: {exc}',
            file_path=fpath,
            spec_ref='Phase 0, check 0.5',
        ))
        return

    if b'\x00' in content:
        pos = content.index(b'\x00')
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='structural',
            code='NULL_BYTE',
            message=f'{mdir.game_id}/{mdir.model_name}/{fname}: null byte at offset {pos}',
            file_path=fpath,
            spec_ref='Phase 0, check 0.5',
        ))

    if '\ufffd'.encode('utf-8') in content:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='structural',
            code='REPLACEMENT_CHAR',
            message=f'{mdir.game_id}/{mdir.model_name}/{fname}: contains Unicode replacement character U+FFFD',
            file_path=fpath,
            spec_ref='Phase 0, check 0.5',
        ))


def _check_encoding(
    fpath: str, mdir: ModelDirInfo, fname: str, findings: list[Finding],
) -> None:
    """Check 0.6: UTF-8 without BOM."""
    try:
        with open(fpath, 'rb') as f:
            head = f.read(3)
    except OSError:
        return  # Already flagged by binary check

    if head[:3] == b'\xef\xbb\xbf':
        findings.append(Finding(
            severity=Severity.MEDIUM,
            phase='structural',
            code='UTF8_BOM',
            message=f'{mdir.game_id}/{mdir.model_name}/{fname}: file has UTF-8 BOM',
            file_path=fpath,
            spec_ref='Phase 0, check 0.6',
        ))

    # Verify valid UTF-8
    try:
        with open(fpath, encoding='utf-8') as f:
            f.read()
    except UnicodeDecodeError as exc:
        findings.append(Finding(
            severity=Severity.CRITICAL,
            phase='structural',
            code='INVALID_UTF8',
            message=f'{mdir.game_id}/{mdir.model_name}/{fname}: not valid UTF-8: {exc}',
            file_path=fpath,
            spec_ref='Phase 0, check 0.6',
        ))


def _check_jsonl_parse(
    fpath: str, mdir: ModelDirInfo, fname: str, findings: list[Finding],
) -> None:
    """Check 0.4: every line parses as valid JSON."""
    try:
        with open(fpath, encoding='utf-8') as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        phase='structural',
                        code='JSONL_PARSE_ERROR',
                        message=(
                            f'{mdir.game_id}/{mdir.model_name}/{fname}:{line_num}: '
                            f'JSON parse error: {exc}'
                        ),
                        file_path=fpath,
                        line_number=line_num,
                        spec_ref='Phase 0, check 0.4',
                    ))
                    # Spec: first failure aborts the check
                    return
    except UnicodeDecodeError:
        pass  # Already flagged by encoding check
