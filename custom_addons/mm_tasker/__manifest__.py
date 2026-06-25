# -*- coding: utf-8 -*-
{
    'name': 'MM Tasker',
    'version': '19.0.5.0.0',
    'category': 'Productivity/AI Evaluation',
    'summary': 'Tasker authoring + QC for multimodal agentic eval (prompts + rubrics.jsonl)',
    'description': """
MM Tasker
=========
Slim workflow module for the multimodal agentic eval pilot:

- Tasker fills default / human / final prompts and uploads rubrics.jsonl
- JSONL is parsed into child rubric rows on upload
- QC reviewer marks pass/fail (with required fail reason)
- QC name + date auto-stamped at QC time
- Tasker sees only own tasks; QC sees all; QC verdict hidden from Tasker
    """,
    'author': 'GRT Labs',
    'license': 'LGPL-3',
    'depends': ['base', 'mail'],
    'data': [
        'security/mm_tasker_security.xml',
        'security/ir.model.access.csv',
        'data/mm_tasker_config.xml',
        'data/ir_cron.xml',
        'views/mm_tasker_run_views.xml',
        'views/mm_tasker_run_wizard_views.xml',
        'views/mm_tasker_task_views.xml',
        'views/mm_tasker_menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
