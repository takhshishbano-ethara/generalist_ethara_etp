from odoo import http
from odoo.http import request
import io
import xlsxwriter
from .utility import validate_token, return_Response, safe_get_value, validate_request
import datetime
from pytz import timezone
from datetime import timedelta
import boto3
import calendar
import time
import uuid
from odoo.tools import html2plaintext
import requests
import re

def _resolve_value(record, path):
    parts = path.split('.')
    cur = record
    for part in parts:
        if not part or not hasattr(cur, '_name'):
            return False
        if part not in cur._fields:
            return False
        cur = cur[part]
        if cur is False or cur is None:
            return False
    return cur


def _xlsx_cell(value):
    if value is False or value is None:
        return ""
    if hasattr(value, '_name'):
        if len(value) == 0:
            return ""
        if len(value) == 1:
            return value.display_name or ""
        return ", ".join((r.display_name or "") for r in value)
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


class ReportXlsxController(http.Controller):

    @validate_token
    @http.route('/api/v1/export_project_project_report', methods=['GET'], type='http', auth='public', csrf=False, cors='*')
    def export_project_project_report(self, **params):
        try:
            user_id = request.env['res.users'].sudo().browse(request.env.uid)
            projects = request.env['project.project'].sudo().search([])
            s3_connector_id = request.env['s3.connector'].sudo().search([], limit=1)
            s3 = boto3.client('s3', aws_access_key_id=s3_connector_id.aws_access_key_id, aws_secret_access_key=s3_connector_id.aws_secret_access_key)

            output = io.BytesIO()
            workbook = xlsxwriter.Workbook(output, {'in_memory': True})
            worksheet = workbook.add_worksheet('Detailed Project Report')
            worksheet.write(0, 0, ('Seq.'))
            worksheet.write(0, 1, ('Name'))
            worksheet.write(0, 2, ('Client Name'))
            worksheet.write(0, 3, ('Project Category'))
            worksheet.write(0, 4, ('Project Type'))
            worksheet.write(0, 5, ('Start Date'))
            worksheet.write(0, 6, ('End Date'))
            worksheet.write(0, 7, ('Status'))
            worksheet.write(0, 8, ('Task Count'))
            worksheet.write(0, 9, ('Team Count'))

            worksheet.set_column('A:A', 15)
            worksheet.set_column('B:B', 20)
            worksheet.set_column('C:C', 15)
            worksheet.set_column('D:D', 30)
            worksheet.set_column('E:E', 20)
            worksheet.set_column('F:F', 15)
            worksheet.set_column('G:G', 35)
            worksheet.set_column('H:H', 30)
            worksheet.set_column('I:I', 15)
            worksheet.set_column('J:J', 15)
            row = 1
            for project in projects:
                team_ids = (project.project_lead.ids + project.project_aire.ids + project.project_swe.ids)
                unique_team_count = len(set(team_ids))
                worksheet.write(row, 0, (safe_get_value(project, 'project_seq', 'str')))
                worksheet.write(row, 1, (safe_get_value(project, 'name', 'str')))
                worksheet.write(row, 2, (safe_get_value(project, 'client_name', 'str')))
                worksheet.write(row, 3, (safe_get_value(project, 'project_category', 'str')))
                worksheet.write(row, 4, (safe_get_value(project, 'project_type', 'str')))
                worksheet.write(row, 5, (safe_get_value(project, 'date_start', 'str')))
                worksheet.write(row, 6, (safe_get_value(project, 'date', 'str')))
                worksheet.write(row, 7, (safe_get_value(project, 'stage_id.name', 'str')))
                worksheet.write(row, 8, (safe_get_value(project, 'sample_task_number', 'int')))
                worksheet.write(row, 9, (unique_team_count))
            workbook.close()
            output.seek(0)
            ts = time.time_ns()
            unique_id = uuid.uuid4().hex[:12]
            filename = f"export_project_project_report_{user_id.id}_{unique_id}_{ts}.xlsx"
            s3.upload_fileobj(output, s3_connector_id.name, f"reports/{filename}", ExtraArgs={'ContentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})
            public_url = f"{s3_connector_id.cdn_url}/reports/{filename}"
            return return_Response(message="Success", status=200, data={"url": public_url})

        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[f"{e}"])

    @validate_token
    @http.route('/api/v1/export_project_connected_table_xlsx', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_request({"project_id": {"type": "int", "required": True}})
    def export_project_connected_table_xlsx(self, **kwargs):
        try:
            jdata = kwargs.get('jdata') or {}
            project_id = int(jdata.get('project_id') or 0)
            fields_request = jdata.get('fields') or []

            if not isinstance(fields_request, list) or not fields_request:
                return return_Response(message="fields must be a non-empty list", status=400)

            project = request.env['project.project'].sudo().browse(project_id)
            if not project.exists():
                return return_Response(message=f"Project {project_id} not found.", status=404)

            table = (getattr(project, 'connected_table', None) or '').strip()
            if not table:
                return return_Response(message="Project has no connected_table value.", status=400)

            if table not in request.env:
                return return_Response(message=f"Model '{table}' is not registered.", status=404)

            Model = request.env[table].sudo()

            columns = []
            for item in fields_request:
                if isinstance(item, dict):
                    name = (item.get('name') or '').strip()
                    if not name:
                        continue
                    header = (item.get('string') or '').strip() or name
                elif isinstance(item, str):
                    name = item.strip()
                    if not name:
                        continue
                    top = name.split('.')[0]
                    header = (Model._fields[top].string if top in Model._fields else top) or top
                else:
                    continue
                columns.append((name, header))

            if not columns:
                return return_Response(message="No valid fields specified.", status=400)

            domain = []
            # if 'project_id' in Model._fields:
            #     domain = [('project_id', '=', project_id)]
            records = Model.search(domain)

            output = io.BytesIO()
            workbook = xlsxwriter.Workbook(output, {'in_memory': True})
            sheet_name = (table.replace('.', '_')[:31]) or 'Records'
            worksheet = workbook.add_worksheet(sheet_name)

            header_fmt = workbook.add_format({'bold': True})
            for col_idx, (_, header) in enumerate(columns):
                worksheet.write(0, col_idx, header, header_fmt)
                worksheet.set_column(col_idx, col_idx, 20)

            for row_idx, rec in enumerate(records, start=1):
                for col_idx, (path, _) in enumerate(columns):
                    worksheet.write(row_idx, col_idx, _xlsx_cell(_resolve_value(rec, path)))

            workbook.close()
            output.seek(0)

            s3_connector = request.env['s3.connector'].sudo().search([], limit=1)
            if not s3_connector:
                return return_Response(message="S3 connector not configured.", status=500)
            s3 = boto3.client(
                's3',
                aws_access_key_id=s3_connector.aws_access_key_id,
                aws_secret_access_key=s3_connector.aws_secret_access_key,
            )
            ts = time.time_ns()
            unique_id = uuid.uuid4().hex[:12]
            filename = f"{table.replace('.', '_')}_project_{project_id}_{unique_id}_{ts}.xlsx"
            s3.upload_fileobj(
                output,
                s3_connector.name,
                f"reports/{filename}",
                ExtraArgs={'ContentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},
            )
            public_url = f"{s3_connector.cdn_url}/reports/{filename}"

            return return_Response(
                message="Success",
                status=200,
                data={
                    "url": public_url,
                    "filename": filename,
                    "project_id": project_id,
                    "connected_table": table,
                    "rows_exported": len(records),
                    "columns": len(columns),
                },
            )
        except Exception as e:
            return return_Response(message="Something Went Wrong.", status=400, errors=[f"{e}"])
