{
    "name": "Rinzler Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Dashboard page for the Rinzler long-horizon agentic-coherence RL environment",
    "description": """
Rinzler Dashboard
=================
A single-page dashboard for the Rinzler reinforcement-learning environment:
30 Harbor-format tasks (across 5 difficulty tiers) that hand a model a written
contract and ask it to run a simulated AI startup for a full 1-year horizon on
the offline yc-bench SQLite sim, graded by a hidden reward-v3 verifier
(13 checkers + continuous scorers, 8 canary tokens per bundle). Available as
both a backend client action and a public portal page at /rinzler.

The dataset viewer and charts are driven entirely by the rinzler-dataset
corpus metadata (declared tier, seed, world config, expected floors/bands);
model rollout / trajectory reward is intentionally NOT surfaced.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/rinzler_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        # Backend bundle (OWL component + SCSS) - loaded when an
        # authenticated user opens the "Rinzler Dashboard" app/menu.
        "web.assets_backend": [
            "rinzler_dashboard/static/src/scss/rinzler_showcase.scss",
            "rinzler_dashboard/static/src/components/showcase/showcase.js",
            "rinzler_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Public /rinzler page assets are NOT bundled into
        # web.assets_frontend on purpose - the portal template serves
        # them as bare <link>/<script> tags so Bootstrap and other
        # portal chrome do not override the dark editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
