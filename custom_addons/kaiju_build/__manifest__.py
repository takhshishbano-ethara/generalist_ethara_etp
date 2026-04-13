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
    "assets": {
        "web.assets_backend": [
            "kaiju_build/static/src/scss/kaiju_build_form.scss",
            "kaiju_build/static/src/components/log_viewer/log_viewer.js",
            "kaiju_build/static/src/components/log_viewer/log_viewer.xml",
            "kaiju_build/static/src/components/log_viewer/log_viewer.scss",
            "kaiju_build/static/src/components/auto_refresh/auto_refresh.js",
            "kaiju_build/static/src/components/auto_refresh/auto_refresh.xml",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
