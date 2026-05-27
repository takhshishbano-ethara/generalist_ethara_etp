# -*- coding: utf-8 -*-
"""Multi-run-per-model schema swap.

Drops UNIQUE(task_id, model_key) and backfills run_index=1 so the new
UNIQUE(task_id, model_key, run_index) created by the ORM stays
satisfied on upgrade. Safe to re-run.
"""


def migrate(cr, version):
    # 1. Ensure run_index exists with a value before the new constraint
    #    lands. Default=1 in the field def covers fresh installs; this
    #    handles rows that predate the column.
    cr.execute(
        """
        ALTER TABLE mm_tasker_run
            ADD COLUMN IF NOT EXISTS run_index INTEGER NOT NULL DEFAULT 1;
        """
    )
    cr.execute(
        "UPDATE mm_tasker_run SET run_index = 1 WHERE run_index IS NULL OR run_index = 0;"
    )

    # 2. Drop the old "one run per (task, model)" constraint. The ORM
    #    will create the new (task, model, run_index) constraint when
    #    the module loads. PostgreSQL auto-names UNIQUE constraints
    #    using the table + columns; check both the model-derived name
    #    and the legacy auto-generated name.
    cr.execute(
        """
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'mm_tasker_run'::regclass
           AND contype = 'u'
           AND pg_get_constraintdef(oid) ILIKE 'UNIQUE (task_id, model_key)';
        """
    )
    for (conname,) in cr.fetchall():
        cr.execute(f'ALTER TABLE mm_tasker_run DROP CONSTRAINT IF EXISTS "{conname}";')
