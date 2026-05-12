#!/usr/bin/env python3
"""Generate all benchmark charts (light + dark variants) for Kraken Dashboard."""

import json
import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# --- Paths ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(SCRIPT_DIR, 'data', 'kraken_instances.json')
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'static', 'src', 'portal', 'img')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- Load Data ---
with open(DATA_PATH, 'r') as f:
    instances = json.load(f)

# --- Color Palette ---
COLOR_GLM5 = '#2E7D32'   # Green
COLOR_NOVA = '#F9A825'   # Yellow
COLOR_EXPERT = '#888888'

# --- Theme configs ---
LIGHT = {
    'fig_bg': 'white',
    'axes_bg': 'white',
    'text_color': 'black',
    'grid_color': '#E0E0E0',
    'spine_color': 'black',
    'tick_color': 'black',
}
DARK = {
    'fig_bg': '#2B2B2B',
    'axes_bg': '#000000',
    'text_color': '#E0E0E0',
    'grid_color': '#4A4A4A',
    'spine_color': 'white',
    'tick_color': '#E0E0E0',
}


def apply_theme(fig, ax, theme, show_grid=True):
    """Apply light/dark theme to figure and axes."""
    fig.patch.set_facecolor(theme['fig_bg'])
    ax.set_facecolor(theme['axes_bg'])
    ax.title.set_color(theme['text_color'])
    ax.xaxis.label.set_color(theme['text_color'])
    ax.yaxis.label.set_color(theme['text_color'])
    ax.tick_params(colors=theme['tick_color'], which='both')
    for spine in ['top', 'right']:
        ax.spines[spine].set_visible(False)
    for spine in ['left', 'bottom']:
        ax.spines[spine].set_color(theme['spine_color'])
    if show_grid:
        ax.yaxis.grid(True, color=theme['grid_color'], linewidth=0.5)
        ax.xaxis.grid(False)
        ax.set_axisbelow(True)
    else:
        ax.grid(False)


def save_chart(fig, filename):
    """Save figure to output directory."""
    path = os.path.join(OUTPUT_DIR, filename)
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved: {filename}")


def strip_repo(instance_id):
    """Strip repo prefix: 'encode__httpx-2423' -> 'httpx-2423'"""
    parts = instance_id.split('__')
    return parts[1] if len(parts) > 1 else instance_id


# =============================================================================
# Chart 1: HSR Harmonic Mean (Bar)
# =============================================================================
def chart_01(theme, suffix):
    fig, ax = plt.subplots(figsize=(10, 8))
    
    models = ['GLM-5', 'Kimi K2.5']
    values = [31.0, 26.8]
    colors = [COLOR_GLM5, COLOR_NOVA]
    
    bars = ax.bar(models, values, width=0.4, color=colors, edgecolor='none')
    
    ax.set_title('Speedup Ratio (HSR) — Harmonic Mean', fontsize=14, fontweight='bold',
                 pad=15, color=theme['text_color'])
    ax.set_ylabel('Harmonic Mean HSR (%)', fontsize=12, color=theme['text_color'])
    ax.set_ylim(0, 35)
    
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.8,
                f'{val:.1f}%', ha='center', va='bottom', fontsize=16, fontweight='bold',
                color=theme['text_color'])
    
    apply_theme(fig, ax, theme)
    save_chart(fig, f'01_hsr_harmonic_mean{suffix}.png')


# =============================================================================
# Chart 2: Outcome Distribution (Line)
# =============================================================================
def chart_02(theme, suffix):
    fig, ax = plt.subplots(figsize=(10, 7))
    
    categories = ['Fail', 'Correct but Slow', 'Pass (SR >= 1)']
    glm5_data = [7/20*100, 8/20*100, 5/20*100]
    nova_data = [9/20*100, 10/20*100, 1/20*100]
    x = np.arange(len(categories))
    
    ax.plot(x, glm5_data, '--', marker='o', markersize=10, color=COLOR_GLM5,
            linewidth=2, label='GLM-5')
    ax.plot(x, nova_data, '-', marker='s', markersize=10, color=COLOR_NOVA,
            linewidth=2, label='Kimi K2.5')
    
    for i, (g, n) in enumerate(zip(glm5_data, nova_data)):
        ax.text(i, g + 2, f'{g:.0f}%', ha='center', va='bottom', fontsize=11,
                fontstyle='italic', color=COLOR_GLM5)
        ax.text(i, n - 2, f'{n:.0f}%', ha='center', va='top', fontsize=11,
                fontstyle='italic', color=COLOR_NOVA)
    
    ax.set_title('Patch Outcome Distribution', fontsize=14, fontweight='bold',
                 pad=15, color=theme['text_color'])
    ax.set_ylabel('Instances (%)', fontsize=12, color=theme['text_color'])
    ax.set_xticks(x)
    ax.set_xticklabels(categories, fontsize=11)
    ax.set_ylim(0, 100)
    ax.set_yticks(range(0, 101, 20))
    ax.legend(loc='upper right', framealpha=0.9, edgecolor=theme['grid_color'])
    
    apply_theme(fig, ax, theme)
    save_chart(fig, f'02_outcome_distribution{suffix}.png')


# =============================================================================
# Chart 3: Per-Instance HSR (Grouped Bar)
# =============================================================================
def chart_03(theme, suffix):
    fig, ax = plt.subplots(figsize=(15, 7))
    
    # Sort by GLM-5 HSR descending
    sorted_instances = sorted(instances, key=lambda x: x['glm5']['hsr'], reverse=True)
    
    labels = [strip_repo(inst['instance_id']) for inst in sorted_instances]
    glm5_hsr = [inst['glm5']['hsr'] for inst in sorted_instances]
    nova_hsr = [inst['nova']['hsr'] for inst in sorted_instances]
    
    x = np.arange(len(labels))
    width = 0.35
    
    ax.bar(x - width/2, glm5_hsr, width, color=COLOR_GLM5, label='GLM-5')
    ax.bar(x + width/2, nova_hsr, width, color=COLOR_NOVA, label='Kimi K2.5')
    
    ax.axhline(y=1.0, color=COLOR_EXPERT, linestyle='--', linewidth=1.5,
               label='Expert baseline (SR=1)')
    
    ax.set_title('Per-Instance HSR Comparison', fontsize=14, fontweight='bold',
                 pad=15, color=theme['text_color'])
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha='right', fontsize=9)
    ax.set_ylabel('HSR', fontsize=12, color=theme['text_color'])
    ax.legend(loc='upper right', framealpha=0.9, edgecolor=theme['grid_color'])
    
    apply_theme(fig, ax, theme)
    save_chart(fig, f'03_per_instance_hsr{suffix}.png')


# =============================================================================
# Chart 4: HSR by Difficulty (Grouped Bar)
# =============================================================================
def chart_04(theme, suffix):
    fig, ax = plt.subplots(figsize=(10, 7))
    
    # Group by difficulty
    difficulty_order = ['Easy', 'Medium', 'Hard', 'Expert']
    groups = {d: {'glm5': [], 'nova': []} for d in difficulty_order}
    
    for inst in instances:
        d = inst['difficulty']
        if d in groups:
            groups[d]['glm5'].append(inst['glm5']['hsr'])
            groups[d]['nova'].append(inst['nova']['hsr'])
    
    counts = {d: len(groups[d]['glm5']) for d in difficulty_order}
    x_labels = [f"{d} (n={counts[d]})" for d in difficulty_order]
    
    glm5_means = [np.mean(groups[d]['glm5']) for d in difficulty_order]
    nova_means = [np.mean(groups[d]['nova']) for d in difficulty_order]
    
    x = np.arange(len(difficulty_order))
    width = 0.35
    
    bars1 = ax.bar(x - width/2, glm5_means, width, color=COLOR_GLM5, label='GLM-5')
    bars2 = ax.bar(x + width/2, nova_means, width, color=COLOR_NOVA, label='Kimi K2.5')
    
    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.02, f'{h:.2f}',
                ha='center', va='bottom', fontsize=10, color=theme['text_color'])
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.02, f'{h:.2f}',
                ha='center', va='bottom', fontsize=10, color=theme['text_color'])
    
    ax.axhline(y=1.0, color=COLOR_EXPERT, linestyle='--', linewidth=1.5)
    
    ax.set_title('Mean HSR by Task Difficulty', fontsize=14, fontweight='bold',
                 pad=15, color=theme['text_color'])
    ax.set_xticks(x)
    ax.set_xticklabels(x_labels, fontsize=11)
    ax.set_ylabel('Mean HSR', fontsize=12, color=theme['text_color'])
    ax.legend(loc='upper right', framealpha=0.9, edgecolor=theme['grid_color'])
    
    apply_theme(fig, ax, theme)
    save_chart(fig, f'04_hsr_by_difficulty{suffix}.png')


# =============================================================================
# Chart 5: Cost vs HSR (Scatter)
# =============================================================================
def chart_05(theme, suffix):
    fig, ax = plt.subplots(figsize=(10, 7))
    
    glm5_cost = [inst['glm5']['cost'] for inst in instances]
    glm5_hsr = [inst['glm5']['hsr_floored'] for inst in instances]
    nova_cost = [inst['nova']['cost'] for inst in instances]
    nova_hsr = [inst['nova']['hsr_floored'] for inst in instances]
    
    ax.scatter(glm5_cost, glm5_hsr, c=COLOR_GLM5, s=80, alpha=0.8,
               edgecolors='none', label='GLM-5', zorder=3)
    ax.scatter(nova_cost, nova_hsr, c=COLOR_NOVA, s=80, alpha=0.8,
               edgecolors='none', label='Kimi K2.5', zorder=3)
    
    ax.axhline(y=1.0, color=COLOR_EXPERT, linestyle='--', linewidth=1.5,
               label='Expert baseline (SR=1)', zorder=2)
    
    ax.set_title('Cost vs. Performance', fontsize=14, fontweight='bold',
                 pad=15, color=theme['text_color'])
    ax.set_xlabel('Cost per Instance ($)', fontsize=12, color=theme['text_color'])
    ax.set_ylabel('HSR (floored)', fontsize=12, color=theme['text_color'])
    ax.legend(loc='upper right', framealpha=0.9, edgecolor=theme['grid_color'])
    
    apply_theme(fig, ax, theme)
    save_chart(fig, f'05_cost_vs_hsr{suffix}.png')


# =============================================================================
# Chart 6: Summary Metrics (Grouped Bar)
# =============================================================================
def chart_06(theme, suffix):
    fig, ax = plt.subplots(figsize=(10, 7))
    
    categories = ['HSR\n(Harmonic Mean)', 'Correct Rate\n(Tests Pass)', 'Pass@1 Rate\n(SR >= 1)']
    glm5_vals = [0.310, 0.65, 0.25]
    nova_vals = [0.268, 0.55, 0.05]
    annotations_glm5 = ['0.310x', '65%', '25%']
    annotations_nova = ['0.268x', '55%', '5%']
    
    x = np.arange(len(categories))
    width = 0.35
    
    bars1 = ax.bar(x - width/2, glm5_vals, width, color=COLOR_GLM5, label='GLM-5')
    bars2 = ax.bar(x + width/2, nova_vals, width, color=COLOR_NOVA, label='Kimi K2.5')
    
    for bar, ann in zip(bars1, annotations_glm5):
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.015, ann,
                ha='center', va='bottom', fontsize=11, fontweight='bold', color=COLOR_GLM5)
    for bar, ann in zip(bars2, annotations_nova):
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.015, ann,
                ha='center', va='bottom', fontsize=11, fontweight='bold', color=COLOR_NOVA)
    
    ax.set_title('Summary Performance Metrics', fontsize=14, fontweight='bold',
                 pad=15, color=theme['text_color'])
    ax.set_xticks(x)
    ax.set_xticklabels(categories, fontsize=11)
    ax.set_ylim(0, 0.8)
    ax.set_yticks(np.arange(0, 0.81, 0.1))
    ax.legend(loc='upper right', framealpha=0.9, edgecolor=theme['grid_color'])
    
    # NO gridlines for this chart
    apply_theme(fig, ax, theme, show_grid=False)
    save_chart(fig, f'06_summary_metrics{suffix}.png')


# =============================================================================
# Main: Generate all charts in both themes
# =============================================================================
def main():
    print("Generating charts...")
    
    charts = [chart_01, chart_02, chart_03, chart_04, chart_05, chart_06]
    
    for chart_fn in charts:
        print(f"\n{chart_fn.__name__}:")
        chart_fn(LIGHT, '')
        chart_fn(DARK, '_dark')
    
    print(f"\nDone! All charts saved to: {OUTPUT_DIR}")


if __name__ == '__main__':
    main()
