# -*- coding: utf-8 -*-
{
    'name': 'Ethara ETP Theme',
    'version': '19.0.1.0.0',
    'category': 'Themes/Backend',
    'summary': 'Ethara-style backend theme with a left-panel menu / submenu navigation',
    'description': """
Ethara ETP Backend Theme
========================
Transforms the Odoo 19 backend into the Ethara ETP look & feel:

* Permanent left sidebar listing every app (menu) and its sub-menus
* Collapsible submenu groups with active-item highlighting
* Collapsible (icon-only) sidebar mode
* Indigo / purple SaaS design language - white cards, light surfaces,
  rounded corners, Inter typeface
* Restyled form, kanban, search, control-panel, dialog and login screens
* Light & dark theme modes
* Favorites - pin any menu and open it from the sidebar or the
  Favorites Home page
* Selectable login page templates (centered, split-screen,
  full-screen background, minimal) with a live preview
* Settings > Ethara Theme panel to customize colors, font, transition
  speed, sidebar width, navbar height, border radius, density, card
  shadow, theme mode and more

The sidebar renders the existing Odoo menu tree through the ``menu`` service;
no new menus are created.
""",
    'author': 'Ethara',
    'website': 'https://web.ethara.ai',
    'depends': ['web', 'base_setup'],
    'data': [
        'security/ir.model.access.csv',
        'security/ethara_security.xml',
        'views/ethara_menu_icon_views.xml',
        'views/ethara_favorites_views.xml',
        'views/res_config_settings_views.xml',
        'views/login_templates.xml',
        'views/webclient_templates.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ethara_etp_theme/static/src/scss/variables.scss',
            'ethara_etp_theme/static/src/scss/theme.scss',
            'ethara_etp_theme/static/src/scss/sidebar.scss',
            'ethara_etp_theme/static/src/scss/views.scss',
            'ethara_etp_theme/static/src/scss/inputs.scss',
            'ethara_etp_theme/static/src/scss/listview.scss',
            'ethara_etp_theme/static/src/scss/darkmode.scss',
            'ethara_etp_theme/static/src/favorites/favorites_home.scss',
            'ethara_etp_theme/static/src/utils/menu_icon.js',
            'ethara_etp_theme/static/src/navbar/navbar_patch.js',
            'ethara_etp_theme/static/src/navbar/navbar.xml',
            'ethara_etp_theme/static/src/favorites/favorites_home.js',
            'ethara_etp_theme/static/src/favorites/favorites_home.xml',
            'ethara_etp_theme/static/src/login/login_preview.scss',
            'ethara_etp_theme/static/src/login/login_template_field.js',
            'ethara_etp_theme/static/src/login/login_template_field.xml',
        ],
        'web.assets_frontend': [
            'ethara_etp_theme/static/src/scss/variables.scss',
            'ethara_etp_theme/static/src/scss/login.scss',
        ],
    },
    'license': 'LGPL-3',
    'installable': True,
    'application': False,
    'auto_install': False,
}
