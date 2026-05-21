def migrate(cr, version):
    cr.execute("""
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'task_forge_response_uniq_task_config'
        AND table_name = 'task_forge_response'
    """)
    if cr.fetchone():
        cr.execute("ALTER TABLE task_forge_response DROP CONSTRAINT task_forge_response_uniq_task_config")
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'task_forge_response' AND column_name = 'config_id'
    """)
    if cr.fetchone():
        cr.execute("ALTER TABLE task_forge_response ALTER COLUMN config_id DROP NOT NULL")
