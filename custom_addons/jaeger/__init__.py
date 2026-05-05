from . import controllers as controllers, models as models, wizard as wizard


def _pre_init_jaeger(env):
    """Ensure schema is ready before ORM validation runs."""
    env.cr.execute("""
        ALTER TABLE jaeger_repository
            ADD COLUMN IF NOT EXISTS prs_jsonl_path VARCHAR,
            ADD COLUMN IF NOT EXISTS filtered_prs_jsonl_path VARCHAR,
            ADD COLUMN IF NOT EXISTS pr_collection_log TEXT,
            ADD COLUMN IF NOT EXISTS pr_collection_step VARCHAR,
            ADD COLUMN IF NOT EXISTS stars INTEGER,
            ADD COLUMN IF NOT EXISTS forks INTEGER,
            ADD COLUMN IF NOT EXISTS is_fork BOOLEAN,
            ADD COLUMN IF NOT EXISTS repo_description TEXT
    """)
    env.cr.execute("""
        UPDATE jaeger_repository SET pipeline_mode = 'swe'
        WHERE pipeline_mode = 'hard_swe'
    """)
    env.cr.execute("""
        UPDATE jaeger_repository SET task_category = 'swe'
        WHERE task_category = 'hard_swe'
    """)
    env.cr.execute("""
        UPDATE jaeger_instance SET task_category = 'swe'
        WHERE task_category = 'hard_swe'
    """)
