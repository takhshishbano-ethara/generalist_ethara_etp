# -*- coding: utf-8 -*-
{
    'name': 'AWS S3 Connector',
    'version': '1.0',
    'category': 'Tools',
    'summary': 'Upload and download files from AWS S3',
    'author': "hyper grocers private limited",
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_config_param.xml',
        'views/s3_connector_views.xml',
        'wizard/upload_image.xml'
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

