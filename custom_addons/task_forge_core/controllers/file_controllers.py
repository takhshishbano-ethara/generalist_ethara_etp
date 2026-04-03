from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response, validate_token, generate_s3_link
)
import base64
import uuid
import time
import mimetypes


MAX_IMAGE_SIZE = 10 * 1024 * 1024   # 10MB
MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100MB
ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/jpg', 'image/webp'}
ALLOWED_VIDEO_TYPES = {'video/mp4', 'video/webm', 'video/quicktime', 'video/x-msvideo', 'video/x-matroska', 'video/avi', 'video/mov'}


class TaskForgeFileController(http.Controller):

    @http.route('/api/v2/taskforge/files/upload', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def upload_file(self, **kwargs):
        try:
            user = request.env.user
            employee = user.employee_id

            file = request.httprequest.files.get('file')
            if not file:
                return return_Response(message="No file provided", status=400)

            content_type = file.content_type or ''
            file_data = file.read()
            file_size = len(file_data)

            # Validate file
            if content_type in ALLOWED_IMAGE_TYPES:
                if file_size > MAX_IMAGE_SIZE:
                    return return_Response(message="Image exceeds 10MB limit", status=400)
            elif content_type in ALLOWED_VIDEO_TYPES:
                if file_size > MAX_VIDEO_SIZE:
                    return return_Response(message="Video exceeds 100MB limit", status=400)
            else:
                return return_Response(message=f"Unsupported file type: {content_type}", status=400)

            # Upload to S3
            b64_data = base64.b64encode(file_data)
            ts = time.time_ns()
            ext = mimetypes.guess_extension(content_type) or ''
            prefix = 'taskforge/files'
            s3_url = generate_s3_link(b64_data, prefix=prefix, uid=employee.id if employee else None)

            # Track file record
            FileRec = request.env['task.forge.file'].sudo()
            record = FileRec.create({
                'name': file.filename,
                'storage_path': s3_url,
                'content_type': content_type,
                'size': file_size,
                'uploaded_by_id': employee.id if employee else False,
            })

            return return_Response(
                message="File uploaded",
                status=200,
                data={'data': {
                    'id': record.id,
                    'url': s3_url,
                    'filename': file.filename,
                    'content_type': content_type,
                    'size': file_size,
                }}
            )
        except Exception as e:
            return return_Response(message=str(e), status=400)

    @http.route('/api/v2/taskforge/files/delete', methods=['POST'], type='http', auth='none', csrf=False, cors='*')
    @validate_token
    def delete_file(self, **kwargs):
        try:
            data = {}
            try:
                import json
                data = json.loads(request.httprequest.data)
            except Exception:
                pass

            file_id = data.get('file_id')
            if not file_id:
                return return_Response(message="file_id required", status=400)

            FileRec = request.env['task.forge.file'].sudo()
            record = FileRec.browse(int(file_id))
            if not record.exists():
                return return_Response(message="File not found", status=404)

            record.write({'is_deleted': True})
            return return_Response(message="File deleted", status=200)
        except Exception as e:
            return return_Response(message=str(e), status=400)
