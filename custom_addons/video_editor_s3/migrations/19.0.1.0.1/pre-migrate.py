# -*- coding: utf-8 -*-
def migrate(cr, version):
    cr.execute(
        "UPDATE video_editor_project SET state = 'draft' "
        "WHERE state IN ('source_downloading', 'source_ready')"
    )
