"""
Generate evaluation charts from JSONL trajectory data.

Plot 1: Score Over Steps (step-function line chart with mean per model)
Plot 2: Score vs Cost (scatter chart, one dot per model mean, connected by dotted line)

Reads steps.jsonl and runs.jsonl from per-model subdirectories within each game folder.
Outputs PNG files to the same directory.

Usage:
    python plot_results.py --game all --data-dir /path/to/session
    python plot_results.py --game ch91 --data-dir /path/to/session
    python plot_results.py --game all --data-dir /path/to/session
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

# ---------------------------------------------------------------------------
# Model colors
# ---------------------------------------------------------------------------

MODEL_COLORS: dict[str, str] = {
    "Claude Opus 4.7":  "#D76F3D",
    "GPT 5.4 Thinking": "#B22222",
    "Gemini 3.1 Pro":   "#1B8A3A",
    "Kimi K2.5":        "#2563EB",
}

_CANONICAL_NAMES = list(MODEL_COLORS.keys())

_MODEL_NAME_MAP: dict[str, str] = {
    "claude opus 4.7":  "Claude Opus 4.7",
    "gpt 5.4 thinking": "GPT 5.4 Thinking",
    "chatgpt 5.4":      "GPT 5.4 Thinking",
    "chatgpt_5.4":      "GPT 5.4 Thinking",
    "gemini 3.1":       "Gemini 3.1 Pro",
    "kimi k2.5":        "Kimi K2.5",
}

_FALLBACK_COLORS = ["#7B61FF", "#E05D5D", "#D4A853", "#5DA5E0", "#8FBE6D"]
_LINE_STYLES = ["solid", "dashed", "dotted", "dashdot"]

logger = logging.getLogger(__name__)


def _normalize_model_name(name: str) -> str:
    if name in MODEL_COLORS:
        return name
    lower = name.lower()
    for prefix in sorted(_MODEL_NAME_MAP, key=len, reverse=True):
        if lower.startswith(prefix):
            return _MODEL_NAME_MAP[prefix]
    return name


def _normalize_records(records: list[dict]) -> list[dict]:
    return [
        {**r, "model": _normalize_model_name(r.get("model", "unknown"))}
        for r in records
    ]


def _get_color(model: str, idx: int = 0) -> str:
    if model in MODEL_COLORS:
        return MODEL_COLORS[model]
    for key, color in MODEL_COLORS.items():
        if key.lower() in model.lower() or model.lower() in key.lower():
            return color
    return _FALLBACK_COLORS[idx % len(_FALLBACK_COLORS)]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            records.append(item)
                        elif isinstance(item, list):
                            # Handle nested arrays (array of arrays of dicts)
                            for sub in item:
                                if isinstance(sub, dict):
                                    records.append(sub)
                elif isinstance(data, dict):
                    records.append(data)
            except json.JSONDecodeError as e:
                logger.warning("Skipping malformed line %d in %s: %s", line_num, path.name, e)
    return records


# ---------------------------------------------------------------------------
# Plot 1: Score Over Steps
# ---------------------------------------------------------------------------

def _count_runs(steps: list[dict]) -> int:
    run_ids = {(s.get("model", ""), s.get("run_number", 0)) for s in steps}
    runs_per_model = defaultdict(int)
    for model, _ in run_ids:
        runs_per_model[model] += 1
    return max(runs_per_model.values()) if runs_per_model else 0


def plot_score_over_steps(
    steps: list[dict],
    output_path: Path,
    game_id: str,
) -> None:
    if not steps:
        logger.warning("No step data — skipping Plot 1")
        return

    by_model_run: dict[str, dict[int, dict[int, float]]] = defaultdict(lambda: defaultdict(dict))
    for s in steps:
        model = s.get("model", "unknown")
        run = s.get("run_number", 0)
        step = s.get("step", 0)
        score = s.get("score", 0.0)
        by_model_run[model][run][step] = score * 100

    if not by_model_run:
        return

    by_model: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for model, runs_dict in by_model_run.items():
        all_steps = set()
        for run_steps in runs_dict.values():
            all_steps.update(run_steps.keys())

        for run_id, run_steps in runs_dict.items():
            run_max = max(run_steps.keys())
            last_score = run_steps[run_max]
            for step in all_steps:
                if step in run_steps:
                    by_model[model][step].append(run_steps[step])
                elif step > run_max:
                    by_model[model][step].append(last_score)

    n_runs = _count_runs(steps)

    fig, ax = plt.subplots(figsize=(16, 7))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    for idx, (model, step_scores) in enumerate(sorted(by_model.items())):
        color = _get_color(model, idx)
        linestyle = _LINE_STYLES[idx % len(_LINE_STYLES)]
        steps_sorted = sorted(step_scores.keys())
        means = [np.mean(step_scores[s]) for s in steps_sorted]
        ax.step(
            steps_sorted, means,
            where="post",
            color=color, linewidth=2.5, alpha=0.9,
            linestyle=linestyle,
        )
        ax.plot(
            steps_sorted[-1], means[-1],
            marker="o", markersize=12,
            markerfacecolor="white", markeredgecolor=color,
            markeredgewidth=3, zorder=5,
        )

    fig.text(
        0.06, 0.97, "Score over steps",
        fontsize=22, fontweight="bold", color="#1a1a1a",
        va="top", ha="left",
    )
    fig.text(
        0.06, 0.92, f"Average across {n_runs} runs  |  {game_id}",
        fontsize=13, color="#888888",
        va="top", ha="left",
    )

    ax.set_xlabel("Step", fontsize=13, fontweight="bold", color="#555555")
    ax.set_ylabel("Score (%)", fontsize=13, fontweight="bold", color="#555555")
    ax.set_ylim(-2, 105)
    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.xaxis.set_major_locator(mticker.MultipleLocator(20))
    ax.tick_params(colors="#777777", labelsize=11, length=0)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#cccccc")
    ax.spines["left"].set_color("#cccccc")

    ax.yaxis.grid(True, color="#e8e8e8", linewidth=0.8)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)

    handles = []
    for idx, (model, _) in enumerate(sorted(by_model.items())):
        color = _get_color(model, idx)
        linestyle = _LINE_STYLES[idx % len(_LINE_STYLES)]
        handles.append(plt.Line2D([0], [0], color=color, linewidth=3, linestyle=linestyle, label=model))
    ax.legend(
        handles=handles, loc="upper left", fontsize=11, frameon=False,
        labelcolor="#444444",
    )

    fig.subplots_adjust(top=0.84, left=0.06, right=0.97, bottom=0.10)
    fig.savefig(output_path, dpi=500, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    logger.info("Plot 1 saved: %s", output_path)


# ---------------------------------------------------------------------------
# Plot 2: Score vs Cost
# ---------------------------------------------------------------------------

def plot_score_vs_cost(
    runs: list[dict],
    output_path: Path,
    game_id: str,
) -> None:
    if not runs:
        logger.warning("No run data — skipping Plot 2")
        return

    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_model[r.get("model", "unknown")].append(r)

    if not by_model:
        return

    model_points: list[tuple[str, float, float]] = []
    for model, run_list in by_model.items():
        mean_cost = np.mean([r.get("cost_usd", 0.0) for r in run_list])
        mean_score = np.mean([r.get("final_score", 0.0) * 100 for r in run_list])
        model_points.append((model, float(mean_cost), float(mean_score)))

    model_points.sort(key=lambda t: t[1])

    fig, ax = plt.subplots(figsize=(16, 7))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    costs = [p[1] for p in model_points]
    scores = [p[2] for p in model_points]

    ax.plot(
        costs, scores,
        color="#bbbbbb", linewidth=2, alpha=0.7, zorder=2,
        linestyle="dotted",
    )

    n_runs = max(len(rl) for rl in by_model.values())

    fig.text(
        0.06, 0.97, "Score vs. mean cost per run",
        fontsize=22, fontweight="bold", color="#1a1a1a",
        va="top", ha="left",
    )
    fig.text(
        0.06, 0.92,
        f"Each point = one model (mean of {n_runs} runs)  |  {game_id}",
        fontsize=13, color="#888888",
        va="top", ha="left",
    )

    ax.set_xlabel("Mean cost per run ($)", fontsize=13, fontweight="bold", color="#555555")
    ax.set_ylabel("Score (%)", fontsize=13, fontweight="bold", color="#555555")

    y_max = min(max(scores) + 15, 105)
    ax.set_ylim(-2, y_max)

    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:.0f}"))
    ax.xaxis.set_major_locator(mticker.MaxNLocator(nbins=20, integer=True))
    ax.tick_params(colors="#777777", labelsize=11, length=0)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#cccccc")
    ax.spines["left"].set_color("#cccccc")

    ax.yaxis.grid(True, color="#e8e8e8", linewidth=0.8)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)

    handles = []
    for idx, (model, _, _) in enumerate(model_points):
        color = _get_color(model, idx)
        handles.append(plt.Line2D(
            [0], [0], marker="o", color="#bbbbbb", linewidth=0,
            markerfacecolor="white", markeredgecolor=color,
            markeredgewidth=3, markersize=10, label=model,
        ))
    legend = ax.legend(
        handles=handles, loc="upper left", fontsize=11, frameon=False,
        labelcolor="#444444",
    )

    fig.subplots_adjust(top=0.84, left=0.06, right=0.97, bottom=0.10)

    fig.canvas.draw()
    legend_bbox = legend.get_window_extent(fig.canvas.get_renderer())
    legend_pad = 6

    display_coords = []
    for model, cost, score in model_points:
        dx, dy = ax.transData.transform((cost, score))
        display_coords.append((dx, dy))

    label_above = [True] * len(model_points)
    MIN_GAP_PX = 100
    for i in range(1, len(model_points)):
        if abs(display_coords[i][0] - display_coords[i - 1][0]) < MIN_GAP_PX:
            label_above[i] = not label_above[i - 1]

    for i, (dx, dy) in enumerate(display_coords):
        lx, ly = dx, dy + 20
        if (legend_bbox.x0 - legend_pad <= lx <= legend_bbox.x1 + legend_pad
                and legend_bbox.y0 - legend_pad <= ly <= legend_bbox.y1 + legend_pad):
            label_above[i] = False

    for idx, (model, cost, score) in enumerate(model_points):
        color = _get_color(model, idx)
        ax.plot(
            cost, score,
            marker="o", markersize=12,
            markerfacecolor="white", markeredgecolor=color,
            markeredgewidth=3, zorder=5,
        )
        above = label_above[idx]
        ax.annotate(
            model, (cost, score),
            textcoords="offset points",
            xytext=(0, 14 if above else -14),
            fontsize=10, color=color,
            ha="center", va="bottom" if above else "top",
            fontweight="bold",
        )

    fig.savefig(output_path, dpi=500, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    logger.info("Plot 2 saved: %s", output_path)


# ---------------------------------------------------------------------------
# Summary Plots
# ---------------------------------------------------------------------------

def plot_summary_score_over_steps(
    all_game_steps: dict[str, list[dict]],
    output_path: Path,
    label: str = "",
) -> None:
    if not all_game_steps:
        return

    model_step_scores: dict[str, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for game_id, steps in all_game_steps.items():
        if not steps:
            continue

        by_model_run: dict[str, dict[int, dict[int, float]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        for s in steps:
            model = s.get("model", "unknown")
            run = s.get("run_number", 0)
            step = s.get("step", 0)
            score = s.get("score", 0.0)
            by_model_run[model][run][step] = score * 100

        for model, runs_dict in by_model_run.items():
            all_steps: set[int] = set()
            for run_steps in runs_dict.values():
                all_steps.update(run_steps.keys())

            step_scores: dict[int, list[float]] = defaultdict(list)
            for run_id, run_steps in runs_dict.items():
                run_max = max(run_steps.keys())
                last_score = run_steps[run_max]
                for step in all_steps:
                    if step in run_steps:
                        step_scores[step].append(run_steps[step])
                    elif step > run_max:
                        step_scores[step].append(last_score)

            for step, scores in step_scores.items():
                model_step_scores[model][step].append(float(np.mean(scores)))

    if not model_step_scores:
        return

    n_games = len([g for g, s in all_game_steps.items() if s])

    fig, ax = plt.subplots(figsize=(16, 7))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    for idx, (model, step_game_scores) in enumerate(sorted(model_step_scores.items())):
        color = _get_color(model, idx)
        linestyle = _LINE_STYLES[idx % len(_LINE_STYLES)]
        steps_sorted = sorted(step_game_scores.keys())
        means = [np.mean(step_game_scores[s]) for s in steps_sorted]

        ax.step(
            steps_sorted, means,
            where="post",
            color=color, linewidth=2.5, alpha=0.9,
            linestyle=linestyle,
        )
        ax.plot(
            steps_sorted[-1], means[-1],
            marker="o", markersize=12,
            markerfacecolor="white", markeredgecolor=color,
            markeredgewidth=3, zorder=5,
        )

    subtitle = f"Average across {n_games} games"
    if label:
        subtitle += f"  |  {label}"

    fig.text(
        0.06, 0.97, "Score over steps",
        fontsize=22, fontweight="bold", color="#1a1a1a",
        va="top", ha="left",
    )
    fig.text(
        0.06, 0.92, subtitle,
        fontsize=13, color="#888888",
        va="top", ha="left",
    )

    ax.set_xlabel("Step", fontsize=13, fontweight="bold", color="#555555")
    ax.set_ylabel("Score (%)", fontsize=13, fontweight="bold", color="#555555")
    ax.set_ylim(-2, 105)
    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.xaxis.set_major_locator(mticker.MultipleLocator(20))
    ax.tick_params(colors="#777777", labelsize=11, length=0)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#cccccc")
    ax.spines["left"].set_color("#cccccc")

    ax.yaxis.grid(True, color="#e8e8e8", linewidth=0.8)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)

    handles = []
    for idx, (model, _) in enumerate(sorted(model_step_scores.items())):
        color = _get_color(model, idx)
        linestyle = _LINE_STYLES[idx % len(_LINE_STYLES)]
        handles.append(plt.Line2D(
            [0], [0], color=color, linewidth=3, linestyle=linestyle, label=model,
        ))
    ax.legend(
        handles=handles, loc="upper left", fontsize=11, frameon=False,
        labelcolor="#444444",
    )

    fig.subplots_adjust(top=0.84, left=0.06, right=0.97, bottom=0.10)
    fig.savefig(output_path, dpi=500, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    logger.info("Summary Plot 1 saved: %s", output_path)


def plot_summary_score_vs_cost(
    all_game_runs: dict[str, list[dict]],
    output_path: Path,
    label: str = "",
) -> None:
    if not all_game_runs:
        return

    model_game_stats: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for game_id, runs in all_game_runs.items():
        if not runs:
            continue
        by_model: dict[str, list[dict]] = defaultdict(list)
        for r in runs:
            by_model[r.get("model", "unknown")].append(r)

        for model, run_list in by_model.items():
            mean_cost = float(np.mean([r.get("cost_usd", 0.0) for r in run_list]))
            mean_score = float(np.mean([r.get("final_score", 0.0) * 100 for r in run_list]))
            model_game_stats[model].append((mean_cost, mean_score))

    if not model_game_stats:
        return

    n_games = len([g for g, r in all_game_runs.items() if r])

    model_points: list[tuple[str, float, float]] = []
    for model, stats in model_game_stats.items():
        avg_cost = float(np.mean([s[0] for s in stats]))
        avg_score = float(np.mean([s[1] for s in stats]))
        model_points.append((model, avg_cost, avg_score))

    model_points.sort(key=lambda t: t[1])

    fig, ax = plt.subplots(figsize=(16, 7))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    costs = [p[1] for p in model_points]
    scores = [p[2] for p in model_points]

    ax.plot(
        costs, scores,
        color="#bbbbbb", linewidth=2, alpha=0.7, zorder=2,
        linestyle="dotted",
    )

    subtitle = f"Average across {n_games} games"
    if label:
        subtitle += f"  |  {label}"

    fig.text(
        0.06, 0.97, "Score vs. mean cost per run",
        fontsize=22, fontweight="bold", color="#1a1a1a",
        va="top", ha="left",
    )
    fig.text(
        0.06, 0.92, subtitle,
        fontsize=13, color="#888888",
        va="top", ha="left",
    )

    ax.set_xlabel("Mean cost per run ($)", fontsize=13, fontweight="bold", color="#555555")
    ax.set_ylabel("Score (%)", fontsize=13, fontweight="bold", color="#555555")

    y_max = min(max(scores) + 15, 105)
    ax.set_ylim(-2, y_max)

    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:.0f}"))
    ax.xaxis.set_major_locator(mticker.MaxNLocator(nbins=20, integer=True))
    ax.tick_params(colors="#777777", labelsize=11, length=0)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#cccccc")
    ax.spines["left"].set_color("#cccccc")

    ax.yaxis.grid(True, color="#e8e8e8", linewidth=0.8)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)

    handles = []
    for idx, (model, _, _) in enumerate(model_points):
        color = _get_color(model, idx)
        handles.append(plt.Line2D(
            [0], [0], marker="o", color="#bbbbbb", linewidth=0,
            markerfacecolor="white", markeredgecolor=color,
            markeredgewidth=3, markersize=10, label=model,
        ))
    legend = ax.legend(
        handles=handles, loc="upper left", fontsize=11, frameon=False,
        labelcolor="#444444",
    )

    fig.subplots_adjust(top=0.84, left=0.06, right=0.97, bottom=0.10)

    fig.canvas.draw()
    legend_bbox = legend.get_window_extent(fig.canvas.get_renderer())
    legend_pad = 6

    display_coords = []
    for model, cost, score in model_points:
        dx, dy = ax.transData.transform((cost, score))
        display_coords.append((dx, dy))

    label_above = [True] * len(model_points)
    MIN_GAP_PX = 100
    for i in range(1, len(model_points)):
        if abs(display_coords[i][0] - display_coords[i - 1][0]) < MIN_GAP_PX:
            label_above[i] = not label_above[i - 1]

    for i, (dx, dy) in enumerate(display_coords):
        lx, ly = dx, dy + 20
        if (legend_bbox.x0 - legend_pad <= lx <= legend_bbox.x1 + legend_pad
                and legend_bbox.y0 - legend_pad <= ly <= legend_bbox.y1 + legend_pad):
            label_above[i] = False

    for idx, (model, cost, score) in enumerate(model_points):
        color = _get_color(model, idx)
        ax.plot(
            cost, score,
            marker="o", markersize=12,
            markerfacecolor="white", markeredgecolor=color,
            markeredgewidth=3, zorder=5,
        )
        above = label_above[idx]
        ax.annotate(
            model, (cost, score),
            textcoords="offset points",
            xytext=(0, 14 if above else -14),
            fontsize=10, color=color,
            ha="center", va="bottom" if above else "top",
            fontweight="bold",
        )

    fig.savefig(output_path, dpi=500, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    logger.info("Summary Plot 2 saved: %s", output_path)


# ---------------------------------------------------------------------------
# Data loading & CLI
# ---------------------------------------------------------------------------

def _filter_by_model(records: list[dict], exclude: list[str]) -> list[dict]:
    if not exclude:
        return records
    lower_excl = [e.lower() for e in exclude]
    return [r for r in records if not any(e in r.get("model", "").lower() for e in lower_excl)]


def _load_game_data(
    game_dir: Path,
    exclude_models: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    steps: list[dict] = []
    runs: list[dict] = []
    found_model_dirs = False
    for model_dir in sorted(game_dir.iterdir()):
        if not model_dir.is_dir():
            continue
        if model_dir.name in ("traces", "logs"):
            continue
        model_steps_path = model_dir / "steps.jsonl"
        model_runs_path = model_dir / "runs.jsonl"
        if model_steps_path.exists() or model_runs_path.exists():
            found_model_dirs = True
            steps.extend(_read_jsonl(model_steps_path))
            runs.extend(_read_jsonl(model_runs_path))

    if not found_model_dirs:
        steps = _read_jsonl(game_dir / "steps.jsonl")
        runs = _read_jsonl(game_dir / "runs.jsonl")

    steps = _normalize_records(steps)
    runs = _normalize_records(runs)

    steps = _filter_by_model(steps, exclude_models or [])
    runs = _filter_by_model(runs, exclude_models or [])
    return steps, runs


def _discover_game_ids(data_dir: Path) -> list[str]:
    if not data_dir.is_dir():
        return []
    skip_dirs = {
        "plot1_score_over_steps", "plot2_score_vs_cost",
        "plot1_all", "plot2_all",
        ".git", "__pycache__", ".DS_Store", "traces", "logs",
    }
    return sorted(
        d.name for d in data_dir.iterdir()
        if d.is_dir()
        and d.name not in skip_dirs
        and not d.name.startswith(".")
        and (
            (d / "steps.jsonl").exists()
            or any(
                (sub / "steps.jsonl").exists()
                for sub in d.iterdir()
                if sub.is_dir() and sub.name not in ("traces", "logs")
            )
        )
    )


def generate_plots(
    game_id: str,
    data_dir: Path,
    output_dir: Path | None = None,
    exclude_models: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    game_dir = data_dir / game_id
    if not game_dir.is_dir():
        logger.error("Game directory not found: %s", game_dir)
        return [], []

    out = output_dir or game_dir
    out.mkdir(parents=True, exist_ok=True)

    steps, runs = _load_game_data(game_dir, exclude_models)

    logger.info("Game %s: %d steps, %d runs", game_id, len(steps), len(runs))

    plot_score_over_steps(steps, out / "plot1_score_over_steps.png", game_id)
    plot_score_vs_cost(runs, out / "plot2_score_vs_cost.png", game_id)

    print(f"  {game_id}: 2 plots saved to {out}/")
    return steps, runs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate evaluation charts from JSONL data",
    )
    parser.add_argument(
        "--game", required=True,
        help="Game ID (e.g., ch91) or 'all' for every game in data dir",
    )
    parser.add_argument(
        "--data-dir", type=Path, required=True,
        help="Directory containing per-game eval data",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Override output directory for PNGs (default: same as game data dir)",
    )
    parser.add_argument(
        "--exclude", nargs="*", default=[],
        help="Exclude models whose name contains any of these substrings",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    print("Generating evaluation plots...")

    if args.game == "all":
        game_ids = _discover_game_ids(args.data_dir)
        if not game_ids:
            logger.error("No games found in %s", args.data_dir)
            sys.exit(1)

        base_dir = args.output_dir or args.data_dir
        plot1_agg_dir = base_dir / "plot1_score_over_steps"
        plot2_agg_dir = base_dir / "plot2_score_vs_cost"
        plot1_agg_dir.mkdir(parents=True, exist_ok=True)
        plot2_agg_dir.mkdir(parents=True, exist_ok=True)

        for gid in game_ids:
            out = args.output_dir / gid if args.output_dir else None
            generate_plots(gid, args.data_dir, out, exclude_models=args.exclude)

            game_out = out or (args.data_dir / gid)
            p1 = game_out / "plot1_score_over_steps.png"
            p2 = game_out / "plot2_score_vs_cost.png"
            if p1.exists():
                shutil.copy2(p1, plot1_agg_dir / f"{gid}.png")
            if p2.exists():
                shutil.copy2(p2, plot2_agg_dir / f"{gid}.png")

        print(f"  Aggregate: {len(game_ids)} games copied to {plot1_agg_dir}/ and {plot2_agg_dir}/")
    else:
        generate_plots(args.game, args.data_dir, args.output_dir, exclude_models=args.exclude)

    print("Done.")


if __name__ == "__main__":
    main()
