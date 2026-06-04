#!/usr/bin/env python3
"""Generate all dashboard charts (light + dark variants) for Castiel Dashboard.

Reads data/castiel_data.json and renders matplotlib PNGs into
static/src/portal/img/ as <name>.png (light) and <name>_dark.png (dark).
All values come straight from the data file — nothing is hardcoded here.
"""

import json
import os

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# --- Paths ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(SCRIPT_DIR, 'data', 'castiel_data.json')
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'static', 'src', 'portal', 'img')
os.makedirs(OUTPUT_DIR, exist_ok=True)

with open(DATA_PATH, 'r', encoding='utf-8') as f:
    DATA = json.load(f)

# --- Theme configs (crimson accent) ---
LIGHT = {
    'fig_bg': 'white', 'axes_bg': 'white', 'text_color': '#0B0E14',
    'grid_color': '#E0E0E0', 'spine_color': '#0B0E14', 'tick_color': '#2F3647',
    'accent': '#C42A3D', 'accent2': '#8E1428', 'bright': '#D72638', 'muted': '#8A90A0',
}
DARK = {
    'fig_bg': '#151A28', 'axes_bg': '#0B0E14', 'text_color': '#F3F5F9',
    'grid_color': '#2A3148', 'spine_color': '#C5CBE9', 'tick_color': '#C5CBE9',
    'accent': '#FF5C6E', 'accent2': '#FF8A9A', 'bright': '#FF3B54', 'muted': '#7A82A0',
}


def apply_theme(fig, ax, theme, show_grid=True, axis='y'):
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
        if axis == 'x':
            ax.xaxis.grid(True, color=theme['grid_color'], linewidth=0.5)
            ax.yaxis.grid(False)
        else:
            ax.yaxis.grid(True, color=theme['grid_color'], linewidth=0.5)
            ax.xaxis.grid(False)
        ax.set_axisbelow(True)
    else:
        ax.grid(False)


def save_chart(fig, filename):
    path = os.path.join(OUTPUT_DIR, filename)
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  Saved: {filename}")


def shade_palette(theme, n):
    """A crimson→slate sequence for categorical charts."""
    base = [theme['bright'], theme['accent'], theme['accent2'], '#C77', '#9AA0B0',
            '#6A7286', '#525A6E', '#3A4156']
    if n <= len(base):
        return base[:n]
    # repeat/extend if needed
    out = list(base)
    while len(out) < n:
        out.append(theme['muted'])
    return out[:n]


def hbar(dct, title, filename_base, sort_desc=True):
    """Horizontal bar chart from an ordered dict {label: count}."""
    items = list(dct.items())
    if sort_desc:
        items.sort(key=lambda kv: kv[1])  # ascending so largest ends on top
    labels = [k for k, _ in items]
    values = [v for _, v in items]

    def render(theme, suffix):
        fig, ax = plt.subplots(figsize=(10, max(4.5, 0.55 * len(labels) + 1.5)))
        colors = shade_palette(theme, len(labels))[::-1]
        bars = ax.barh(labels, values, color=colors, edgecolor='none')
        ax.set_title(title, fontsize=14, fontweight='bold', pad=14, color=theme['text_color'])
        ax.set_xlim(0, max(values) * 1.18)
        for bar, val in zip(bars, values):
            ax.text(bar.get_width() + max(values) * 0.02, bar.get_y() + bar.get_height() / 2,
                    str(val), va='center', ha='left', fontsize=11, fontweight='bold',
                    color=theme['text_color'])
        ax.tick_params(axis='y', labelsize=10)
        apply_theme(fig, ax, theme, axis='x')
        save_chart(fig, f'{filename_base}{suffix}.png')

    render(LIGHT, '')
    render(DARK, '_dark')


def vbar(dct, title, ylabel, filename_base, annotate_suffix=''):
    """Vertical bar chart from an ordered dict {label: value}."""
    labels = list(dct.keys())
    values = list(dct.values())

    def render(theme, suffix):
        fig, ax = plt.subplots(figsize=(9, 6.5))
        colors = shade_palette(theme, len(labels))
        bars = ax.bar(labels, values, width=0.6, color=colors, edgecolor='none')
        ax.set_title(title, fontsize=14, fontweight='bold', pad=14, color=theme['text_color'])
        ax.set_ylabel(ylabel, fontsize=12, color=theme['text_color'])
        ax.set_ylim(0, max(values) * 1.18)
        for bar, val in zip(bars, values):
            txt = f'{val}{annotate_suffix}'
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(values) * 0.02,
                    txt, ha='center', va='bottom', fontsize=13, fontweight='bold',
                    color=theme['text_color'])
        apply_theme(fig, ax, theme)
        save_chart(fig, f'{filename_base}{suffix}.png')

    render(LIGHT, '')
    render(DARK, '_dark')


def donut(dct, title, filename_base):
    labels = list(dct.keys())
    values = list(dct.values())

    def render(theme, suffix):
        fig, ax = plt.subplots(figsize=(8, 6.5))
        colors = shade_palette(theme, len(labels))
        wedges, _ = ax.pie(values, colors=colors, startangle=90,
                           wedgeprops=dict(width=0.42, edgecolor=theme['fig_bg'], linewidth=2))
        ax.set_title(title, fontsize=14, fontweight='bold', pad=14, color=theme['text_color'])
        total = sum(values)
        legend_labels = [f'{l}  ·  {v}  ({v / total * 100:.0f}%)' for l, v in zip(labels, values)]
        ax.legend(wedges, legend_labels, loc='center left', bbox_to_anchor=(0.92, 0.5),
                  frameon=False, fontsize=11, labelcolor=theme['text_color'])
        fig.patch.set_facecolor(theme['fig_bg'])
        ax.set_facecolor(theme['fig_bg'])
        ax.title.set_color(theme['text_color'])
        save_chart(fig, f'{filename_base}{suffix}.png')

    render(LIGHT, '')
    render(DARK, '_dark')


def benchmark_scale(filename_base):
    benches = DATA['benchmarks']
    pairs = [(b['name'].replace(' (seed)', '\n(seed)'), b['instances'])
             for b in benches if b.get('instances')]
    labels = [p[0] for p in pairs]
    values = [p[1] for p in pairs]

    def render(theme, suffix):
        fig, ax = plt.subplots(figsize=(10, 6.5))
        colors = [theme['accent2'] if 'seed' in l.lower() else theme['muted'] for l in labels]
        # highlight Castiel in bright crimson
        colors = [theme['bright'] if 'castiel' in l.lower() else theme['accent'] for l in labels]
        bars = ax.bar(range(len(labels)), values, width=0.6, color=colors, edgecolor='none')
        ax.set_yscale('log')
        ax.set_title('Benchmark scale — real-world vuln instances (log scale)',
                     fontsize=14, fontweight='bold', pad=14, color=theme['text_color'])
        ax.set_ylabel('Instances (log)', fontsize=12, color=theme['text_color'])
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=10)
        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 1.05,
                    f'{val:,}', ha='center', va='bottom', fontsize=12, fontweight='bold',
                    color=theme['text_color'])
        apply_theme(fig, ax, theme)
        save_chart(fig, f'{filename_base}{suffix}.png')

    render(LIGHT, '')
    render(DARK, '_dark')


def cybergym_models(filename_base):
    rows = sorted(DATA['cybergym_models'], key=lambda r: r['success'])
    labels = [r['model'] for r in rows]
    values = [r['success'] for r in rows]

    def render(theme, suffix):
        fig, ax = plt.subplots(figsize=(11, 7.5))
        colors = [theme['bright'] if v == max(values) else theme['accent'] for v in values]
        bars = ax.barh(labels, values, color=colors, edgecolor='none')
        ax.set_title('CyberGym — Level-1 success rate by model (OpenHands)',
                     fontsize=14, fontweight='bold', pad=14, color=theme['text_color'])
        ax.set_xlabel('Success rate (%)', fontsize=12, color=theme['text_color'])
        ax.set_xlim(0, max(values) * 1.18)
        for bar, val in zip(bars, values):
            ax.text(bar.get_width() + max(values) * 0.02, bar.get_y() + bar.get_height() / 2,
                    f'{val:.1f}%', va='center', ha='left', fontsize=10, fontweight='bold',
                    color=theme['text_color'])
        ax.tick_params(axis='y', labelsize=10)
        apply_theme(fig, ax, theme, axis='x')
        save_chart(fig, f'{filename_base}{suffix}.png')

    render(LIGHT, '')
    render(DARK, '_dark')


def cybergym_difficulty(filename_base):
    rows = DATA['cybergym_difficulty']
    labels = [r['level'] for r in rows]
    values = [r['success'] for r in rows]

    def render(theme, suffix):
        fig, ax = plt.subplots(figsize=(9, 6.5))
        # gradient by level (darker → brighter crimson)
        colors = shade_palette(theme, len(labels))[::-1]
        bars = ax.bar(labels, values, width=0.6, color=colors, edgecolor='none')
        ax.set_title('CyberGym — success rate vs. input information',
                     fontsize=14, fontweight='bold', pad=14, color=theme['text_color'])
        ax.set_ylabel('Success rate (%)', fontsize=12, color=theme['text_color'])
        ax.set_ylim(0, max(values) * 1.25)
        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(values) * 0.02,
                    f'{val:.1f}%', ha='center', va='bottom', fontsize=13, fontweight='bold',
                    color=theme['text_color'])
        apply_theme(fig, ax, theme)
        save_chart(fig, f'{filename_base}{suffix}.png')

    render(LIGHT, '')
    render(DARK, '_dark')


def main():
    print("Generating Castiel charts...")
    c = DATA['corpus']

    print("\n01 bug_classes:");      hbar(c['bug_classes'], 'Seed corpus — bug classes (n=20)', '01_bug_classes')
    print("02 languages:");         donut(c['languages'], 'Seed corpus — languages (n=20)', '02_languages')
    print("03 difficulty:");        vbar(c['difficulty'], 'Seed corpus — difficulty (n=20)', 'Instances', '03_difficulty')
    print("04 attack_surface:");    hbar(c['attack_surface'], 'Seed corpus — attack surface (n=20)', '04_attack_surface')
    print("05 categories:");        hbar(c['categories'], 'Seed corpus — project categories (n=20)', '05_categories')
    print("06 benchmark_scale:");   benchmark_scale('06_benchmark_scale')
    print("07 cybergym_models:");   cybergym_models('07_cybergym_models')
    print("08 cybergym_difficulty:"); cybergym_difficulty('08_cybergym_difficulty')

    print(f"\nDone! All charts saved to: {OUTPUT_DIR}")


if __name__ == '__main__':
    main()
