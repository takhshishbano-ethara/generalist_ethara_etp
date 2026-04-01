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
