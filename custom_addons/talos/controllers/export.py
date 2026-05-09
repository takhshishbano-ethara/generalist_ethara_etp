# -*- coding: utf-8 -*-
import csv
import io
import json
import operator

from werkzeug.exceptions import InternalServerError

from odoo import http
from odoo.exceptions import UserError
from odoo.http import content_disposition, request
from odoo.tools import osutil

from odoo.addons.web.controllers.export import CSVExport


class TalosCSVExport(CSVExport):

    _csv_batch_size = 5000

    @http.route('/web/export/csv', type='http', auth='user')
    def web_export_csv(self, data):
        try:
            params = json.loads(data)
            model = params.get('model', '')

            if not model.startswith('talos.'):
                return super().web_export_csv(data)

            model, fields, ids, domain, import_compat = \
                operator.itemgetter('model', 'fields', 'ids', 'domain', 'import_compat')(params)

            Model = request.env[model].with_context(import_compat=import_compat, **params.get('context', {}))
            if not Model._is_an_ordinary_table():
                fields = [field for field in fields if field['name'] != 'id']

            field_names = [f['name'] for f in fields]
            if import_compat:
                columns_headers = field_names
            else:
                columns_headers = [val['label'].strip() for val in fields]

            records = Model.browse(ids) if ids else Model.search(domain)

            groupby = params.get('groupby')
            if not import_compat and groupby:
                raise UserError(request.env._("Exporting grouped data to csv is not supported."))

            headers = [
                ('Content-Disposition', content_disposition(
                    osutil.clean_filename(self.filename(model) + self.extension))),
                ('Content-Type', self.content_type),
            ]

            return request.make_response(
                self._generate_csv_stream(records, field_names, columns_headers),
                headers=headers,
            )
        except Exception as exc:
            if isinstance(exc, (UserError, InternalServerError)):
                raise
            from odoo.http import serialize_exception
            import logging
            logging.getLogger(__name__).exception("Exception during CSV export.")
            payload = json.dumps({
                'code': 0,
                'message': "Odoo Server Error",
                'data': serialize_exception(exc)
            })
            raise InternalServerError(payload) from exc

    def _generate_csv_stream(self, records, field_names, columns_headers):
        fp = io.StringIO()
        writer = csv.writer(fp, quoting=1)
        writer.writerow(columns_headers)
        yield fp.getvalue()
        fp.seek(0)
        fp.truncate()

        record_ids = records.ids
        batch_size = self._csv_batch_size

        for offset in range(0, len(record_ids), batch_size):
            batch = records.browse(record_ids[offset:offset + batch_size])
            export_data = batch.export_data(field_names).get('datas', [])

            for data in export_data:
                row = []
                for d in data:
                    if d is None or d is False:
                        d = ''
                    elif isinstance(d, bytes):
                        d = d.decode()
                    if isinstance(d, str) and d.startswith(('=', '-', '+')):
                        d = "'" + d
                    row.append(d)
                writer.writerow(row)

            yield fp.getvalue()
            fp.seek(0)
            fp.truncate()

        fp.close()
