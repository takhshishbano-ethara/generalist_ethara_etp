{
    'name': 'Castiel Dashboard',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Exploit-Evidence Cyber Range — Corpus & Benchmark Dashboard',
    'description': """
        Castiel Dashboard
        =================
        Interactive dashboard for Project Castiel (Ethara AI): a deterministic,
        contamination-resistant cyber range for training and evaluating models that
        produce exploit-evidence Proof-of-Concept artifacts against vulnerable code.

        Surfaces the project's real data — the 20-task Harbor-validated seed corpus
        (bug-class / language / difficulty / attack-surface composition), the
        Evidence-Tier verifier, the competitive benchmark landscape (CyberGym, ARVO,
        BountyBench), and the P0–P4 roadmap. Baseline model runs are marked as roadmap
        until populated; no per-task or model figures are fabricated.
    """,
    'author': 'Ethara',
    'website': 'https://www.ethara.ai',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'web',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/castiel_dashboard_menus.xml',
        'views/portal_templates.xml',
    ],
    'assets': {
        # Backend bundle (OWL component + SCSS) — loaded when an authenticated
        # user opens the "Castiel" app/menu.
        'web.assets_backend': [
            'castiel_dashboard/static/src/scss/castiel_showcase.scss',
            'castiel_dashboard/static/src/components/showcase/showcase.js',
            'castiel_dashboard/static/src/components/showcase/showcase.xml',
        ],
        # The public /castiel page serves its CSS/JS as bare <link>/<script>
        # tags from the portal template (see views/portal_templates.xml), so it
        # is intentionally NOT bundled into web.assets_frontend.
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
