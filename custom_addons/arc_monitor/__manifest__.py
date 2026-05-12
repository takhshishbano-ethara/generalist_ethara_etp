{
    'name': 'ARC Monitor',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Live monitoring dashboard for ARC-AGI puzzle evaluation runs',
    'description': """
        Odoo port of puzzle_monitor.py — reads filesystem eval data and
        presents a live-refreshing dashboard with filters, detail modals,
        timing/token stats, and session log viewer.

        Features:
        - Auto-scan via cron (5-min interval with mtime check)
        - Card grid with status icons, stat tiles, search/filter/sort
        - Detail modal: overview, timing stats, token totals, per-run table,
          steps table with expandable reasoning/observation/notepad, skips, session log
        - Manual scan trigger from UI
    """,
    'author': 'Ethara',
    'website': '',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'depends': [
        'base',
        'mail',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/monitor_scan_views.xml',
        'views/menu.xml',
    ],
    'demo': [],
    'assets': {
        'web.assets_backend': [
            'arc_monitor/static/src/dashboard/arc_monitor_dashboard.js',
            'arc_monitor/static/src/dashboard/arc_monitor_dashboard.xml',
            'arc_monitor/static/src/dashboard/arc_monitor_dashboard.scss',
        ],
    },
}
