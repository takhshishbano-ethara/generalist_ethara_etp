"""Core types for the ARC Bench Analytics engine. Zero Odoo imports."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RunData:
    """Parsed data from a single run in runs.jsonl."""
    run_id: str
    model: str
    game_id: str
    run_number: int
    total_steps: int
    max_steps: int
    final_score_pct: float
    solved: bool
    levels_completed: int
    total_levels: int
    cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    elapsed_seconds: float
    error: str | None = None


@dataclass(slots=True)
class StepData:
    """Parsed data from a single step in steps.jsonl."""
    run_id: str
    model: str
    game_id: str
    run_number: int
    step: int
    score_pct: float
    cumulative_cost_usd: float
    level: int
    total_levels: int
    done: bool


@dataclass(slots=True)
class ModelAnalysis:
    """Aggregated analysis for a single model within a game."""
    model_name: str
    model_dir: str
    run_count: int
    mean_score_pct: float
    mean_cost_usd: float
    total_steps: int
    solved_count: int
    mean_elapsed_seconds: float
    # Per-step average score across runs (length = max_steps)
    score_over_steps: list[float] = field(default_factory=list)
    runs: list[RunData] = field(default_factory=list)


@dataclass(slots=True)
class GameAnalysis:
    """Analysis result for a single game directory."""
    game_id: str
    game_path: str
    models: list[ModelAnalysis] = field(default_factory=list)
    plot1_png: bytes = b''
    plot2_png: bytes = b''


@dataclass(slots=True)
class AnalysisConfig:
    """Configuration for an analysis run."""
    expected_runs_per_model: int = 3
    max_steps: int = 200


@dataclass(slots=True)
class AnalysisResult:
    """Top-level result of a complete analysis run."""
    games: list[GameAnalysis] = field(default_factory=list)
    game_count: int = 0
    model_count: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    script_stdout: str = ''
    script_stderr: str = ''
