"""Plotter — generate matplotlib plots as PNG bytes."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import ModelAnalysis

MODEL_COLORS = {
    'Claude Opus 4.7': '#E67E22',
    'GPT 5.4 Thinking': '#8B0000',
    'Gemini 3.1 Pro': '#27AE60',
    'Kimi K2.5': '#2980B9',
}

MODEL_LINESTYLES = {
    'Claude Opus 4.7': '-',
    'GPT 5.4 Thinking': '--',
    'Gemini 3.1 Pro': ':',
    'Kimi K2.5': '-',
}

_FALLBACK_COLORS = ['#9B59B6', '#E74C3C', '#1ABC9C', '#F39C12', '#34495E']


def _get_color(model_name: str, idx: int) -> str:
    return MODEL_COLORS.get(model_name, _FALLBACK_COLORS[idx % len(_FALLBACK_COLORS)])


def _get_linestyle(model_name: str) -> str:
    return MODEL_LINESTYLES.get(model_name, '-')


def generate_plot1(
    game_id: str,
    models: list[ModelAnalysis],
    max_steps: int = 200,
) -> bytes:
    """Generate Plot 1: Score over steps (step/staircase line chart).

    Uses drawstyle='steps-post' for sharp 90-degree transitions.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mtick

    fig, ax = plt.subplots(figsize=(14, 7))

    for idx, model in enumerate(models):
        if not model.score_over_steps:
            continue
        color = _get_color(model.model_name, idx)
        linestyle = _get_linestyle(model.model_name)
        steps_range = list(range(len(model.score_over_steps)))
        ax.plot(
            steps_range,
            model.score_over_steps,
            label=model.model_name,
            color=color,
            linestyle=linestyle,
            linewidth=2.0,
            drawstyle='steps-post',
        )
        if model.score_over_steps:
            ax.plot(
                len(model.score_over_steps) - 1,
                model.score_over_steps[-1],
                'o',
                color=color,
                markersize=8,
                markerfacecolor='white',
                markeredgewidth=2,
            )

    ax.set_xlabel('Step', fontsize=12)
    ax.set_ylabel('Score (%)', fontsize=12)
    fig.suptitle('Score over steps', fontsize=16, fontweight='bold', x=0.12, ha='left', y=0.98)
    ax.set_title(
        f'Average across {models[0].run_count if models else 3} runs  |  {game_id}',
        fontsize=10,
        color='gray',
        loc='left',
    )
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(xmax=100.0))
    ax.set_xlim(0, max_steps)
    ax.set_ylim(0, 100)
    ax.legend(loc='upper left', framealpha=0.9)
    ax.grid(True, alpha=0.3)
    ax.tick_params(axis='x', rotation=0)
    ax.tick_params(axis='y', rotation=0)

    plt.tight_layout(rect=(0.0, 0.0, 1.0, 0.95))
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generate_plot2(
    game_id: str,
    models: list[ModelAnalysis],
) -> bytes:
    """Generate Plot 2: Score vs. mean cost per run (scatter chart).

    Points connected by a dotted line in order of increasing cost.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mtick

    fig, ax = plt.subplots(figsize=(14, 7))

    sorted_models = sorted(models, key=lambda m: m.mean_cost_usd)

    # Dotted line connecting all points (sorted by cost, left to right)
    costs = [m.mean_cost_usd for m in sorted_models]
    scores = [m.mean_score_pct for m in sorted_models]
    ax.plot(
        costs,
        scores,
        linestyle=':',
        color='#AAAAAA',
        linewidth=1.5,
        zorder=2,
    )

    for idx, model in enumerate(sorted_models):
        color = _get_color(model.model_name, idx)
        ax.scatter(
            model.mean_cost_usd,
            model.mean_score_pct,
            s=150,
            color='white',
            edgecolors=color,
            linewidths=2.5,
            zorder=5,
            label=model.model_name,
        )
        ax.annotate(
            model.model_name,
            (model.mean_cost_usd, model.mean_score_pct),
            textcoords='offset points',
            xytext=(0, 15),
            ha='center',
            fontsize=10,
            fontweight='bold',
            color=color,
        )

    ax.set_xlabel('Mean cost per run ($)', fontsize=12)
    ax.set_ylabel('Score (%)', fontsize=12)
    fig.suptitle('Score vs. mean cost per run', fontsize=16, fontweight='bold', x=0.12, ha='left', y=0.98)
    ax.set_title(
        f'Each point = one model (mean of {models[0].run_count if models else 3} runs)  |  {game_id}',
        fontsize=10,
        color='gray',
        loc='left',
    )
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(xmax=100.0))
    ax.xaxis.set_major_formatter(mtick.FuncFormatter(lambda x, _: f'${x:,.0f}'))
    ax.legend(loc='upper left', framealpha=0.9)
    ax.grid(True, alpha=0.3)
    ax.tick_params(axis='x', rotation=0)
    ax.tick_params(axis='y', rotation=0)

    plt.tight_layout(rect=(0.0, 0.0, 1.0, 0.95))
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()
