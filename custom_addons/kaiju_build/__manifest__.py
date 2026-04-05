# -*- coding: utf-8 -*-
{
    "name": "Kaiju Build Platform",
    "version": "19.0.1.0.0",
    "category": "Technical",
    "summary": "Multi-arch Docker image builds via buildx on EKS",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/kaiju_security.xml",
        "security/ir.model.access.csv",
        "data/cron.xml",
        "views/kaiju_app_views.xml",
        "views/kaiju_build_views.xml",
        "views/kaiju_menus.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
