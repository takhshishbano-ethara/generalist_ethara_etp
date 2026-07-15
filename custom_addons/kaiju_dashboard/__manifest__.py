{
    "name": "Kaiju Dashboard",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Dashboard for the Kaiju from-scratch library-generation RL environment",
    "description": """
Kaiju Dashboard
===============
A single-page dashboard for the Kaiju reinforcement-learning environment:
whole-library generation from a natural-language specification. Each instance
drops a coding agent into a hermetic container with a spec PDF and build
infrastructure (Dockerfile + setup scripts) and asks it to implement a
complete Python library from scratch, graded by a held-out test suite whose
stage-level pass fraction is the scalar reward. A single frontier model
(Opus 4.8) is calibrated across a three-stage refinement pipeline
(draft, lint, test). Available as both a backend client action and a public
portal page at /kaiju.
    """,
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/kaiju_dashboard_menus.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "kaiju_dashboard/static/src/scss/kaiju_showcase.scss",
            "kaiju_dashboard/static/src/components/showcase/showcase.js",
            "kaiju_dashboard/static/src/components/showcase/showcase.xml",
        ],
        # Portal CSS/JS served as bare <link>/<script> in the template
        # to avoid Bootstrap/portal chrome interference with the
        # dark editorial design.
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
