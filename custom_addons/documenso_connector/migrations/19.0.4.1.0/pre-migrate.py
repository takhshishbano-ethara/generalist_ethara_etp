def migrate(cr, version):
    obsolete_xml_ids = [
        'documenso_connector.menu_documenso_signed_all',
        'documenso_connector.menu_documenso_signed_contracts',
        'documenso_connector.menu_documenso_signed_compliance',
        'documenso_connector.menu_documenso_signed_offers',
        'documenso_connector.menu_documenso_signed_repo',
        'documenso_connector.menu_documenso_send_contract',
        'documenso_connector.menu_documenso_documents',
        'documenso_connector.menu_documenso_recipients',
        'documenso_connector.menu_documenso_fields',
        'documenso_connector.menu_documenso_upload',
        'documenso_connector.menu_documenso_sync',
        'documenso_connector.menu_documenso_configuration',
    ]
    for xml_id in obsolete_xml_ids:
        module, name = xml_id.split('.', 1)
        cr.execute(
            "SELECT res_id FROM ir_model_data WHERE module = %s AND name = %s AND model = 'ir.ui.menu'",
            (module, name),
        )
        row = cr.fetchone()
        if not row:
            continue
        menu_id = row[0]
        cr.execute("DELETE FROM ir_ui_menu WHERE parent_id = %s", (menu_id,))
        cr.execute("DELETE FROM ir_ui_menu WHERE id = %s", (menu_id,))
        cr.execute(
            "DELETE FROM ir_model_data WHERE module = %s AND name = %s",
            (module, name),
        )
