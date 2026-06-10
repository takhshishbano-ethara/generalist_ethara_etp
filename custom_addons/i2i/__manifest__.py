{
    "name": "I2I - Image-to-Image QC",
    "version": "19.0.1.12.1",
    "category": "Operations/Quality",
    "summary": "Image-to-Image Generalist project with LLM-assisted QC review",
    "author": "Ethara",
    "license": "LGPL-3",
    "depends": ["base", "web", "mail"],
    "external_dependencies": {
        "python": ["cryptography", "requests", "openpyxl"],
    },
    "data": [
        "security/i2i_security.xml",
        "security/ir.model.access.csv",
        "security/i2i_record_rules.xml",
        "data/i2i_sequence.xml",
        "data/ir_cron.xml",
        "wizards/i2i_import_wizard_views.xml",
        "wizards/i2i_export_wizard_views.xml",
        "views/i2i_project_views.xml",
        "views/i2i_item_views.xml",
        "views/i2i_qc_views.xml",
        "views/res_config_settings_views.xml",
        "views/i2i_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "i2i/static/src/scss/i2i_image_zoom.scss",
            "i2i/static/src/js/i2i_image_zoom.js",
        ],
    },
    "application": True,
    "installable": True,
}
