"""Quick verification with all checks enabled (including content safety)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine import run_qc, QcConfig
from engine.types import Verdict


def main():
    fixtures_dir = os.path.join(os.path.dirname(__file__), 'fixtures')

    valid_path = os.path.join(fixtures_dir, 'valid_game')
    result = run_qc(valid_path, QcConfig())

    print(f'Full scan (with content safety + smell tests):')
    print(f'  Verdict: {result.verdict.value}')
    print(f'  Findings: C={result.critical_count} H={result.high_count} M={result.medium_count} L={result.low_count}')
    print(f'  Duration: {result.duration_seconds:.3f}s')

    if result.findings:
        print(f'\n  Unexpected findings ({len(result.findings)}):')
        for f in result.findings[:10]:
            print(f'    [{f.severity.value.upper()}] {f.phase}/{f.code}: {f.message[:120]}')

    assert result.verdict == Verdict.SHIP, f'Expected SHIP, got {result.verdict.value}'
    print('\n  PASS: Full scan returns SHIP ✓')


if __name__ == '__main__':
    main()
