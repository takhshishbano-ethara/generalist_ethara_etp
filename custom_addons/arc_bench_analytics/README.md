# ARC Bench Analytics

Odoo module for ingesting ARC-AGI-3 LLM evaluation trajectory data and generating per-game performance visualizations.

## Features

- Parses `runs.jsonl` and `steps.jsonl` per model per game
- Computes aggregated metrics (mean score, mean cost, elapsed time)
- Generates two plots per game via an external Python script (subprocess):
  - **Plot 1**: Score over steps (step-function line chart, all models averaged across runs)
  - **Plot 2**: Score vs. mean cost per run (scatter chart, one point per model)
- Stores plots in 3 locations:
  1. Per-game folder (alongside source data)
  2. Aggregate folders (`plot1_score_over_steps/`, `plot2_score_vs_cost/`) with `{game_id}.png` naming
  3. Odoo Binary fields (for UI display)
- Git integration: clone → generate plots → commit & push back
- Local directory: generate plots in-place (source directory is never deleted)

## Expected Data Structure

```
session_dir/
├── av01/
│   ├── Claude_Opus_4.7/
│   │   ├── runs.jsonl      # 1 JSON object per line (one per run)
│   │   └── steps.jsonl     # 1 JSON array per line (one array per run, 200 step objects each)
│   ├── GPT_5.4_Thinking/
│   │   ├── runs.jsonl
│   │   └── steps.jsonl
│   ├── Gemini_3.1_Pro/
│   │   └── ...
│   └── Kimi_K2.5/
│       └── ...
├── ch91/
│   └── ...
└── ... (25 game folders)
```

## Output Structure

After running analysis:

```
session_dir/
├── av01/
│   ├── plot1_score_over_steps.png
│   └── plot2_score_vs_cost.png
├── ch91/
│   └── ...
├── plot1_score_over_steps/        # All game Plot 1s in one folder
│   ├── av01.png
│   ├── ch91.png
│   └── ...
└── plot2_score_vs_cost/           # All game Plot 2s in one folder
    ├── av01.png
    ├── ch91.png
    └── ...
```

## Configuration

| Field | Description |
|-------|-------------|
| Source Type | Local directory or Git repository |
| Session Directory | Absolute path to the data folder |
| Plot Script | Path to the Python plotting script (leave blank for bundled default) |
| Expected Runs | Expected number of runs per model (default: 3) |
| Max Steps | Maximum steps per run (default: 200) |

## Plot Script

The module delegates plot generation to an external Python script via subprocess. This allows:

- Swapping plotting logic without modifying the Odoo module
- Testing plots from the command line independently
- Zero matplotlib dependency in the Odoo worker process

**Bundled default**: `static/scripts/plot_results.py`

**CLI usage** (standalone):
```bash
python plot_results.py --game all --data-dir /path/to/session
python plot_results.py --game ch91 --data-dir /path/to/session
```

**Requirements** for the script environment:
- Python 3.10+
- matplotlib
- numpy

## Module Structure

```
arc_bench_analytics/
├── __manifest__.py
├── engine/                     # Pure Python (no Odoo imports)
│   ├── types.py                # Dataclasses: RunData, StepData, ModelAnalysis, etc.
│   ├── discovery.py            # Walk dirs, find games + model subdirs
│   ├── parser.py               # Parse runs.jsonl + steps.jsonl
│   ├── aggregator.py           # Compute mean metrics, score timeseries
│   └── runner.py               # Orchestrate: discover → parse → subprocess → collect PNGs
├── models/                     # Odoo ORM layer
│   ├── arc_bench_session.py    # Top-level session (source config, background thread, git)
│   ├── arc_bench_game_result.py# Per-game result with Binary plot fields
│   ├── arc_bench_model_result.py# Per-model aggregated metrics
│   └── arc_bench_run.py        # Individual run records
├── views/
│   ├── arc_bench_session_views.xml
│   ├── arc_bench_game_result_views.xml
│   └── menu_items.xml
├── security/
│   ├── security.xml            # Groups, privileges, record rules
│   └── ir.model.access.csv    # Model access rights
└── static/scripts/
    └── plot_results.py         # Bundled default plotting script
```

## Dependencies

- `base`
- `mail`

## License

LGPL-3
