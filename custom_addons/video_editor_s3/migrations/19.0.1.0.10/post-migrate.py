import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Collapse 3-role security model to 2 roles: promote former editor-group members to manager (preserves their see-all access; the editor group XML is removed by module update)."""
    cr.execute("""
        SELECT res_id FROM ir_model_data
        WHERE module = 'video_editor_s3'
          AND name = 'group_video_editor_s3_editor'
          AND model = 'res.groups'
    """)
    row = cr.fetchone()
    if not row:
        _logger.info("Editor group already absent; nothing to migrate.")
        return
    editor_gid = row[0]

    cr.execute("""
        SELECT res_id FROM ir_model_data
        WHERE module = 'video_editor_s3'
          AND name = 'group_video_editor_s3_manager'
          AND model = 'res.groups'
    """)
    row = cr.fetchone()
    if not row:
        _logger.warning("Manager group missing; cannot migrate editors.")
        return
    manager_gid = row[0]

    cr.execute("""
        INSERT INTO res_groups_users_rel (gid, uid)
        SELECT %s, uid FROM res_groups_users_rel WHERE gid = %s
        ON CONFLICT DO NOTHING
    """, (manager_gid, editor_gid))
    promoted = cr.rowcount
    _logger.info("Promoted %d editor user(s) to manager group.", promoted)

    cr.execute("DELETE FROM res_groups_users_rel WHERE gid = %s", (editor_gid,))
    _logger.info("Cleared %d editor membership row(s).", cr.rowcount)
