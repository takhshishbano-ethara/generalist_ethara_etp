# MILO-RL

RL training pipeline for long-horizon coding tasks on MILO-bench, extending [verl](https://github.com/volcengine/verl).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Stage 0: Data Preparation                              │
│  MILO decompose → augment → validate → difficulty score │
└───────────────────────────┬─────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────┐
│  Stage 1: RFT Warmup                                    │
│  Rejection sampling → SFT on passing trajectories       │
└───────────────────────────┬─────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────┐
│  Stage 2: GRPO + DAPO Training (via verl)               │
│  ScalingInter-RL curriculum │ Docker grading │ Monitoring│
└───────────────────────────┬─────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────┐
│  Evaluation                                             │
│  Per-PR pass@1/pass@8 │ Best-of-N reranking             │
└─────────────────────────────────────────────────────────┘
```

## Prerequisites

- Python 3.10+
- Docker (for code evaluation sandboxes)
- 8× H100 80GB (full training) or 2× A100 (development/eval)

## Installation

```bash
pip install -e ".[dev]"
```

## Quick Start

```bash
# Stage 0: Prepare training data
python scripts/run_stage0.py --config configs/stage0.yaml

# Stage 1: RFT warmup
python scripts/run_stage1.py --config configs/stage1_rft.yaml

# Stage 2: GRPO training (Hydra-based, uses verl)
python scripts/run_stage2.py

# Evaluation
python scripts/run_eval.py --config configs/eval.yaml
```

## Development

```bash
make install   # Install with dev dependencies
make all       # Run lint + typecheck + tests
make test-fast # Quick test run (stop on first failure)
```

## Configuration

All configs live in `configs/`. Uses Hydra for training (Stage 2) and plain YAML for other stages.

Override any setting via CLI:
```bash
python scripts/run_stage2.py grpo.learning_rate=1e-5 curriculum.phases.0.max_turns=15
```

## Project Structure

```
src/
├── core/          # Pydantic schemas + typed config
├── data/          # Data pipeline (decompose, augment, validate, dataset)
├── training/      # MiloTrainer, RFT warmup, curriculum, reward manager
├── rollout/       # Docker executor + tool integration
├── eval/          # Per-PR evaluation + Best-of-N
└── monitoring/    # Metrics, kill conditions, rollout replay
```
