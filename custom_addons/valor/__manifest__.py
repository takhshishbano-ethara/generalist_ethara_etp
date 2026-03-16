# -*- coding: utf-8 -*-
{
    'name': "valor",

    'summary': "Multiturn Tasks",

    'description': """
Long description of module's purpose
    """,

    'author': "My Company",
    'website': "https://www.yourcompany.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/15.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '19.0.1.0.0',
    'application': True,
    'post_init_hook': '_migrate_flat_turns_to_one2many',

    # any module necessary for this one to work correctly
    'depends': ['base', 'web', 'hr'],

    # always loaded
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/data.xml',
        'views/valor_turn_views.xml',
        'views/views.xml',
        'views/domain_levels_views.xml',
        'views/templates.xml',
        'views/menu_item.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'valor/static/src/views/fields/markdown_latex_field/markdown_latex_field.js',
            'valor/static/src/views/fields/markdown_latex_field/markdown_latex_field.xml',
            'valor/static/src/views/fields/markdown_latex_field/markdown_latex_field.scss',
            'valor/static/src/views/widgets/evaluate_button/evaluate_button.js',
            'valor/static/src/views/widgets/evaluate_button/evaluate_button.xml',
            'valor/static/src/views/widgets/evaluate_button/evaluate_button.scss',
        ],
    },

    # only loaded in demonstration mode
    'demo': [
        'demo/demo.xml',
    ],
}

