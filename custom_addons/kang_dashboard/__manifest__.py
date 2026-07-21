{
    "name": "Kang Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Dashboard for Kang (Hedge-Bench 1.0), a financial-reasoning benchmark for AI agents",
    "description": """
Kang Dashboard
==============
A single-page dashboard for Kang (Hedge-Bench 1.0), a financial-reasoning
benchmark of 102 on-the-job tasks grounded in the explicit reasoning traces
of professional hedge-fund analysts. Each task casts an agent as an analyst
with a corpus of primary documents (SEC filings, earnings calls, financials)
and an open-ended theme, then grades its reasoning deterministically against
verified expert moves on a dense 0-4 rubric. Eight frontier models were tested
across 6,528 trials; the best captures under half the rubric. Available as both
a backend client action and a public portal page at /kang.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/kang_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "kang_dashboard/static/src/scss/kang_showcase.scss",
            "kang_dashboard/static/src/components/showcase/showcase.js",
            "kang_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Portal CSS/JS served as bare <link>/<script> in the template
        # to avoid Bootstrap/portal chrome interference with the
        # dark editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
