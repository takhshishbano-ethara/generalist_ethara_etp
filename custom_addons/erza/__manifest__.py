{
    "name": "Erza",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Public Erza portal (/erza) - paired-Δ efficacy + frontier-model dataset viewer",
    "description": """
Erza
====

Public showcase at **/erza**. A fully independent sibling of `erza_dashboard`
(which serves the samples page at /erza-samples) - no cross-module dependency
and no shared model tables: this module owns its own `erza.bench.task` /
`erza.bench.run` / `erza.bench.model` ORM models, seeded from its own bundled
data, so the two modules can be installed or removed independently.

The portal renders the paired-Δ Agent-Skills story (sections 01-07, driven by
the seeded models via /erza/api/*) plus two frontier-benchmark sections:

* 08 · Dataset viewer  - 10,000 instances, 3 models, 90,000 runs; a filterable,
  paginated, expandable per-instance table (Opus 4.8 / GPT 5.5 / Gemini 3.1 Pro),
  fed by a bundled sample JSON (data/erza_instances.json).
* 09 · Model comparison - head-to-head per-model breakdown cards.

Benchmark figures are placeholders pending the first public run.
""",
    "author": "Ethara.AI",
    "website": "https://www.ethara.ai",
    "license": "LGPL-3",
    "depends": ["base", "web", "website"],
    "data": [
        "security/ir.model.access.csv",
        "data/seed.xml",
        "views/portal_templates.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
