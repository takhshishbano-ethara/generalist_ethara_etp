{
    'name': 'Talos',
    'version': '19.0.1.0.0',
    'category': 'Tools',
    'summary': 'Talos module',
    'description': """
        Talos custom module for Ethara ETP.
    """,
    'author': 'Ethara',
    'depends': ['base', 'web'],
    'data': [
        'security/ir.model.access.csv',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
