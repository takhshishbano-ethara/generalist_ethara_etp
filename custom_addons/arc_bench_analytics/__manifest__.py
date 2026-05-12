{
    'name': 'ARC Bench Analytics',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Benchmark analytics and visualization for ARC-AGI-3 LLM evaluations',
    'description': """
        Ingests trajectory data from ARC-AGI-3 eval sessions and generates
        per-game performance visualizations comparing LLM models.

        Features:
        - Parses runs.jsonl and steps.jsonl per model per game
        - Computes aggregated metrics (mean score, mean cost, score-over-steps)
        - Generates two plots per game:
            Plot 1: Score over steps (line chart, all models)
            Plot 2: Score vs. mean cost per run (scatter chart)
        - Stores plots in 3 locations:
            1. Per-game folder (alongside source data)
            2. Aggregate folders (plot1_all/, plot2_all/) with game_id naming
            3. Odoo Binary fields (for UI display)
        - Git integration: clone, generate plots, commit & push back
        - Local directory: generate plots in-place
    """,
    'author': 'Ethara',
    'website': '',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'depends': [
        'base',
        'mail',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/arc_bench_session_views.xml',
        'views/arc_bench_game_result_views.xml',
        'views/menu_items.xml',
    ],
    'demo': [],
}
