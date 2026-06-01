# -*- coding: utf-8 -*-
"""Post-migration for 19.0.7.0.0 — intentionally empty.

The 19.0.7.0.0 bump only adds:

* the ``gemini`` preset in ``model_name`` Selection on ``kaiju.commit0.run``
* removal of the ``CHANGE-ME-generate-a-secure-token`` default for
  ``kaiju.webhook_token`` (handled in ``pre-migrate.py``)
* documentation of the Argo Workflows v3.4+ requirement in the manifest

None of those need post-load ORM work.  The earlier pass@k schema
(``kaiju.commit0.run.sample``, ``passk_*`` columns) was reverted before
shipping, so there is no backfill to perform.

Kept as a placeholder so the migrations/19.0.7.0.0/ directory continues
to exist in case follow-up patches under the same version need it.
"""


def migrate(cr, version):
    pass
