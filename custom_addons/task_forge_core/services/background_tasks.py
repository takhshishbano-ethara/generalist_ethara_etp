import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import odoo

from odoo.modules.registry import Registry
_logger = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='ethara_bg')


def run_in_background(db_name, uid, func_name, model_name, record_id, args=None):
    _pool.submit(_execute, db_name, uid, func_name, model_name, record_id, args or {})


def run_many_in_background(db_name, uid, tasks):
    for task in tasks:
        _pool.submit(
            _execute,
            db_name,
            uid,
            task['func'],
            task['model'],
            task['record_id'],
            task.get('args', {}),
        )


def _execute(db_name, uid, func_name, model_name, record_id, args):
    thread_name = threading.current_thread().name
    _logger.info('%s: running %s.%s(id=%s)', thread_name, model_name, func_name, record_id)
    try:
        # registry = odoo.registry(db_name)
        registry = Registry(db_name)
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, uid, {})
            record = env[model_name].browse(record_id)
            if not record.exists():
                _logger.warning('%s: record %s(%s) not found', thread_name, model_name, record_id)
                return

            method = getattr(record, func_name, None)
            if not method:
                _logger.error('%s: method %s not found on %s', thread_name, func_name, model_name)
                return

            if args:
                method(**args)
            else:
                method()
            cr.commit()
            _logger.info('%s: %s.%s(id=%s) completed', thread_name, model_name, func_name, record_id)
    except Exception as e:
        _logger.error('%s: %s.%s(id=%s) failed: %s', thread_name, model_name, func_name, record_id, str(e))


def send_mail_background(db_name, uid, mail_values):
    _pool.submit(_send_mail, db_name, uid, mail_values)


def _send_mail(db_name, uid, mail_values):
    thread_name = threading.current_thread().name
    _logger.info('%s: sending email to %s', thread_name, mail_values.get('email_to', ''))
    try:
        # registry = odoo.registry(db_name)
        registry = Registry(db_name)
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, uid, {})
            mail = env['mail.mail'].sudo().create(mail_values)
            mail.send()
            cr.commit()
            _logger.info('%s: email sent to %s', thread_name, mail_values.get('email_to', ''))
    except Exception as e:
        _logger.error('%s: email failed: %s', thread_name, str(e))


def send_notification_background(db_name, uid, notif_values):
    _pool.submit(_send_notification, db_name, uid, notif_values)


def _send_notification(db_name, uid, notif_values):
    thread_name = threading.current_thread().name
    try:
        # registry = odoo.registry(db_name)
        registry = Registry(db_name)
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, uid, {})
            env['kubera.notification'].sudo().create(notif_values)
            cr.commit()
    except Exception as e:
        _logger.error('%s: notification failed: %s', thread_name, str(e))
