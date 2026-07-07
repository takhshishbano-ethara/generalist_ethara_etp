def migrate(cr, version):
    cr.execute("DROP TABLE IF EXISTS ethara_job_application CASCADE")

    cr.execute("""
        DELETE FROM ir_ui_view WHERE model = 'ethara.job.application'
    """)
    cr.execute("""
        DELETE FROM ir_ui_menu WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'ethara_hrms_extension'
              AND model = 'ir.ui.menu'
              AND name = 'menu_ethara_job_application'
        )
    """)
    cr.execute("""
        DELETE FROM ir_act_window WHERE res_model = 'ethara.job.application'
    """)
    cr.execute("""
        DELETE FROM ir_model_fields_selection WHERE field_id IN (
            SELECT id FROM ir_model_fields WHERE model = 'ethara.job.application'
        )
    """)
    cr.execute("""
        DELETE FROM ir_model_fields WHERE model = 'ethara.job.application'
    """)
    cr.execute("""
        DELETE FROM ir_model_access WHERE model_id IN (
            SELECT id FROM ir_model WHERE model = 'ethara.job.application'
        )
    """)
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE module = 'ethara_hrms_extension'
          AND (model = 'ethara.job.application'
               OR name LIKE '%ethara_job_application%')
    """)
    cr.execute("""
        DELETE FROM ir_model WHERE model = 'ethara.job.application'
    """)
