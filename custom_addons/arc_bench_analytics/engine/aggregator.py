"""Aggregator — compute mean metrics and step timeseries from parsed data."""

from __future__ import annotations

from .types import ModelAnalysis, RunData, StepData


def aggregate_model(
    model_dir: str,
    runs: list[RunData],
    steps_by_run: list[list[StepData]],
    max_steps: int = 200,
) -> ModelAnalysis:
    """Aggregate data for a single model across all its runs.

    Args:
        model_dir: Directory name (e.g. 'Claude_Opus_4.7').
        runs: Parsed run data for this model.
        steps_by_run: Parsed step data per run.
        max_steps: Expected maximum steps per run.

    Returns:
        ModelAnalysis with averaged metrics and score-over-steps timeseries.
    """
    if not runs:
        model_name = model_dir.replace('_', ' ')
        return ModelAnalysis(
            model_name=model_name,
            model_dir=model_dir,
            run_count=0,
            mean_score_pct=0.0,
            mean_cost_usd=0.0,
            total_steps=0,
            solved_count=0,
            mean_elapsed_seconds=0.0,
        )

    model_name = runs[0].model or model_dir.replace('_', ' ')
    run_count = len(runs)
    mean_score_pct = sum(r.final_score_pct for r in runs) / run_count
    mean_cost_usd = sum(r.cost_usd for r in runs) / run_count
    total_steps = sum(r.total_steps for r in runs)
    solved_count = sum(1 for r in runs if r.solved)
    mean_elapsed = sum(r.elapsed_seconds for r in runs) / run_count

    # Build score-over-steps: for each step index, average score_pct across runs
    score_over_steps = []
    for step_idx in range(max_steps):
        step_scores = []
        for run_steps in steps_by_run:
            if step_idx < len(run_steps):
                step_scores.append(run_steps[step_idx].score_pct)
        if step_scores:
            score_over_steps.append(sum(step_scores) / len(step_scores))
        else:
            score_over_steps.append(0.0)

    return ModelAnalysis(
        model_name=model_name,
        model_dir=model_dir,
        run_count=run_count,
        mean_score_pct=mean_score_pct,
        mean_cost_usd=mean_cost_usd,
        total_steps=total_steps,
        solved_count=solved_count,
        mean_elapsed_seconds=mean_elapsed,
        score_over_steps=score_over_steps,
        runs=runs,
    )
