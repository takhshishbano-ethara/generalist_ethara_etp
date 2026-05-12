# ARC QC

Automated QC validation module for ARC-AGI-3 trajectory deliveries. Pure Python, deterministic (no LLM) — runs field-level checks, invariant validation, content safety scans, and smell tests against trajectory data produced by the arc-explainer eval harness.

Implements the **Golden QC System Prompt v2.1** specification exactly, plus 8 false-positive filters for known ARC puzzle data patterns.

## Features

- **Git Integration** — Clone trajectory repos directly (URL + token + branch) or point at a local directory
- **7-Phase QC Engine** — Discovery, Structural, Runs Validation, Steps Validation, Cross-Run Consistency, Content Safety, Smell Tests
- **Verdicts** — SHIP / CONDITIONAL SHIP / BLOCK based on severity thresholds
- **OWL Dashboard** — KPIs, model health bars, verdict breakdown, per-game grid, session trend
- **Per-Game Results** — Individual verdict per game with phase-by-phase pass/fail summary
- **Findings** — Batched insert, grouped by code, filterable by severity/phase/game

## Module Structure

```
arc_qc/
├── engine/                  # Pure Python QC engine (no Odoo imports)
│   ├── types.py             # Dataclasses: Finding, QcResult, GameResult, QcConfig
│   ├── schemas.py           # Canonical models, required fields, regex patterns
│   ├── verdict.py           # Severity counting → verdict computation
│   ├── runner.py            # Orchestrates all phases
│   └── phases/
│       ├── discovery.py     # Tree walk, model dir detection
│       ├── structural.py    # File existence, JSONL parse, encoding checks
│       ├── runs_validation.py   # 25 required fields, invariants §6.4.1-6.4.7
│       ├── steps_validation.py  # 22 required fields, action allow-list, sequences, state transitions, score/level/cost invariants
│       ├── cross_run.py     # run_id linkage, step count vs total_steps, token sum verification
│       ├── content_safety.py    # ~40 regex patterns (leakage, injection, PII) + 8 false-positive filters
│       └── smell_tests.py   # Cookie-cutter CoT, synthetic timestamps, zero tokens, empty-field dumps
├── models/
│   ├── arc_qc_session.py    # Main model: source config, QC execution, results
│   ├── arc_qc_game_result.py    # Per-game verdict and counts
│   ├── arc_qc_model_result.py   # Per-model-dir metadata
│   └── arc_qc_finding.py   # Individual QC findings with severity/phase/code
├── controllers/
│   └── main.py              # Dashboard JSON endpoint
├── static/src/dashboard/
│   ├── arc_qc_dashboard.js  # OWL component (client action)
│   ├── arc_qc_dashboard.xml # Template
│   └── arc_qc_dashboard.scss    # Styles
├── views/                   # Session, Game Result, Finding views + menus
├── security/                # Groups, ACLs, record rules
└── tests/
    ├── generate_fixtures.py # Synthetic valid/invalid test data generator
    ├── test_engine.py       # Engine validation against fixtures
    └── test_full_scan.py    # Full scan with content safety + smell tests
```

## QC Spec Compliance

Validates against the **Golden QC System Prompt v2.1** specification:

| Check | Details |
|-------|---------|
| Canonical Models | Claude_Opus_4.7, Gemini_3.1_Pro, GPT_5.4_Thinking, Kimi_K2.5 |
| runs.jsonl | 25 required fields, type=run_complete, 7 invariants (§6.4.1-6.4.7) |
| steps.jsonl | 22 required fields, 0-indexed steps, action allow-list |
| Actions | UP, DOWN, LEFT, RIGHT, RESET, SELECT, CLICK X Y, UNDO |
| States | NOT_FINISHED, WIN, GAME_OVER |
| State Transitions | Legal table enforced: NOT_FINISHED→{NOT_FINISHED, WIN, GAME_OVER}, WIN→terminal, GAME_OVER→{GAME_OVER, NOT_FINISHED}. RESET from any state→NOT_FINISHED always legal. |
| Score Monotonicity | Non-decreasing; drops only when: (a) curr=RESET + level=1 + (prev=RESET OR just arrived at new level), or (b) prev_state=GAME_OVER + score→0 — CRITICAL (§7.3) |
| RESET State Consistency | score=0 + level>1 on RESET = impossible state (unless prev_state=GAME_OVER) — CRITICAL (§7.4) |
| Score-Level Formula | score == (level-1)/total_levels on non-RESET, non-post-RESET steps — CRITICAL (§7.4) |
| Level Progression | 0/+1 only; drops same rule as score + allowed after GAME_OVER; never exceeds total_levels — CRITICAL |
| Done Terminality | No steps after done=true |
| Cost Additivity | cumulative_cost[N] >= cumulative_cost[N-1] + step_cost[N] |
| Token Sum | run.total_tokens >= Σ(step.tokens) per run |
| Content Safety | ~40 regex patterns: project leakage, prompt injection, PII, contamination (§10) |
| Smell Tests | Cookie-cutter CoT, empty reasoning, zero tokens (non-Kimi), synthetic timestamps, linear cost, empty-field dumps (§12) |

## False-Positive Filters

8 filters suppress known ARC puzzle data patterns that match content safety regexes:

| # | Pattern | Filter Logic |
|---|---------|-------------|
| 1 | Credit card regex | Suppress if matched text is space-separated integers all ≤30 (puzzle grid data) |
| 2 | `SYSTEM:` injection | Suppress if preceded by alphabetic character (compound noun: "NAVIGATION SYSTEM:") |
| 3 | "actions are being recorded" | Suppress if preceded by speculation prefix (my/if my/whether my) |
| 4 | Base64 pattern | Suppress if matched string (minus trailing =) is all digits |
| 5 | "act as a/an/the" | Only flag if followed by AI/injection role noun (assistant/bot/hacker etc.) |
| 6 | "new instructions" | Suppress if within ±80 chars of game context (level/next/puzzle/stage/game/round) |
| 7 | Citation markers `[N]`/`(NNNN)` | Suppress grid values ≤30, non-year patterns, high bracket density |
| 8 | Zero reasoning tokens | Claude/Gemini/Kimi exempt (model-characteristic behavior) |

## Verdict Rules

| Condition | Result |
|-----------|--------|
| 1+ CRITICAL finding | BLOCK |
| 2+ HIGH findings | BLOCK |
| 4+ MEDIUM findings | BLOCK |
| 6+ LOW findings | BLOCK |
| 3 MEDIUM or 5 LOW | CONDITIONAL SHIP |
| Otherwise | SHIP |

## Severity Levels

| Level | Meaning |
|-------|---------|
| CRITICAL | Data corruption, impossible states, PII exposure |
| HIGH | Structural violations, missing required data |
| MEDIUM | Content safety matches, invariant edge cases |
| LOW | Weak signals, citation patterns, smell test warnings |

## Dependencies

- `base`, `mail`, `arc_eval_launcher`
- Python stdlib only (no external packages beyond Odoo)

## Usage

1. Install the module (`-i arc_qc`)
2. Navigate to **ARC QC > QC Sessions**
3. Create a new session:
   - **Git mode**: Enter repo URL, token, branch
   - **Local mode**: Enter filesystem path to trajectory data
4. Click **Run QC** — runs in background thread
5. View results: Summary tab, Game Results tab, Findings tab
6. Dashboard shows aggregate KPIs across all sessions

## Standalone Engine Usage

The engine can be used without Odoo:

```python
from engine.runner import run_qc
from engine.types import QcConfig

result = run_qc("/path/to/batch_directory")
print(result.verdict)        # SHIP / CONDITIONAL_SHIP / BLOCK
print(result.total_findings) # Total finding count
print(result.counts)         # {CRITICAL: N, HIGH: N, MEDIUM: N, LOW: N}
```

## Reference Documentation

- `QC_INFO.md` — Full technical reference (all fields, patterns, filters, thresholds)
- `SPEC_COMPLIANCE.md` — Spec compliance notes and false-positive filter rationale
