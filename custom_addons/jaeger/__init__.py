from . import controllers as controllers, models as models, wizard as wizard


def _migrate_hard_swe(env):
    """Migrate hard_swe → swe before Selection values are validated."""
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
