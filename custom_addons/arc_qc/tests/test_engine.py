"""Quick standalone verification of the QC engine against test fixtures."""

import os
import sys

# Add the parent path so we can import the engine directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine import run_qc, QcConfig
from engine.types import Verdict, Severity


def main():
    fixtures_dir = os.path.join(os.path.dirname(__file__), 'fixtures')

    # --- Test 1: Valid game should SHIP ---
    print('=' * 60)
    print('TEST 1: valid_game → expecting SHIP')
    print('=' * 60)

    valid_path = os.path.join(fixtures_dir, 'valid_game')
    result = run_qc(valid_path, QcConfig(skip_content_safety=True))

    print(f'  Verdict: {result.verdict.value}')
    print(f'  Games: {result.games_checked}, Models: {result.models_checked}')
    print(f'  Runs: {result.runs_checked}, Steps: {result.steps_checked}')
    print(f'  Findings: C={result.critical_count} H={result.high_count} M={result.medium_count} L={result.low_count}')
    print(f'  Duration: {result.duration_seconds:.3f}s')

    if result.findings:
        print(f'\n  FINDINGS ({len(result.findings)}):')
        for f in result.findings[:20]:
            print(f'    [{f.severity.value.upper()}] {f.phase}/{f.code}: {f.message[:100]}')
        if len(result.findings) > 20:
            print(f'    ... and {len(result.findings) - 20} more')

    # --- Test 2: Invalid game should BLOCK ---
    print('\n' + '=' * 60)
    print('TEST 2: invalid_game → expecting BLOCK')
    print('=' * 60)

    invalid_path = os.path.join(fixtures_dir, 'invalid_game')
    result2 = run_qc(invalid_path, QcConfig(skip_content_safety=True))

    print(f'  Verdict: {result2.verdict.value}')
    print(f'  Games: {result2.games_checked}, Models: {result2.models_checked}')
    print(f'  Runs: {result2.runs_checked}, Steps: {result2.steps_checked}')
    print(f'  Findings: C={result2.critical_count} H={result2.high_count} M={result2.medium_count} L={result2.low_count}')
    print(f'  Duration: {result2.duration_seconds:.3f}s')

    if result2.findings:
        print(f'\n  FINDINGS ({len(result2.findings)}):')
        for f in result2.findings[:30]:
            print(f'    [{f.severity.value.upper()}] {f.phase}/{f.code}: {f.message[:120]}')
        if len(result2.findings) > 30:
            print(f'    ... and {len(result2.findings) - 30} more')

    # --- Summary ---
    print('\n' + '=' * 60)
    print('SUMMARY')
    print('=' * 60)
    pass_1 = result.verdict == Verdict.SHIP
    pass_2 = result2.verdict == Verdict.BLOCK
    print(f'  Test 1 (valid → SHIP): {"PASS ✓" if pass_1 else "FAIL ✗"}')
    print(f'  Test 2 (invalid → BLOCK): {"PASS ✓" if pass_2 else "FAIL ✗"}')

    if not (pass_1 and pass_2):
        sys.exit(1)
    print('\n  All tests passed!')


if __name__ == '__main__':
    main()
