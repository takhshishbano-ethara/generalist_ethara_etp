# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.54.0.

The single-invitation mail template ships with ``noupdate="1"``, so a module
upgrade never rewrites its stored ``body_html``. Patch the live record in place
so the invitation stops referencing the dropped ``category_id`` and shows the
question generator instead.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("SAVEPOINT etp_cat_mail")
    try:
        cr.execute("""
            UPDATE mail_template
               SET body_html = REPLACE(
                       REPLACE(body_html::text,
                               'object.assessment_id.category_id.name',
                               'object.assessment_id.generator_id.name'),
                       '<b>Category:</b>', '<b>Question Generator:</b>')::jsonb
             WHERE id IN (
                 SELECT res_id FROM ir_model_data
                  WHERE module = 'etp_assessment_pro'
                    AND model = 'mail.template'
                    AND name = 'mail_template_single_invitation')
        """)
    except Exception as exc:  # noqa: BLE001 - guarded, logged, rolled back
        cr.execute("ROLLBACK TO SAVEPOINT etp_cat_mail")
        _logger.warning("post-migrate 19.0.1.54.0: mail template patch "
                        "skipped: %s", exc)
    else:
        cr.execute("RELEASE SAVEPOINT etp_cat_mail")
        _logger.info("post-migrate 19.0.1.54.0: repointed invitation template "
                     "from category_id to generator_id (%s row).", cr.rowcount)
