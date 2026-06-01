# -*- coding: utf-8 -*-
{
    'name': 'YouTube Video Download',
    'version': '19.0.1.0.0',
    'category': 'Tools',
    'summary': 'Download YouTube video segments using yt-dlp',
    'description': 'Provides a UI to download a section of a YouTube video '
                   'between a given start and end time using yt-dlp.',
    'author': 'Ethara',
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'views/yt_video_download_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
    'auto_install': False,
}
