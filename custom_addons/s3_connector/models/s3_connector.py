from botocore.exceptions import ClientError
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
import os
import io
import mimetypes
import requests
from urllib.parse import urlparse
import boto3
import re


class S3Connector(models.Model):
    _name = 's3.connector'
    _description = 'AWS S3 Connector'

    name = fields.Char(string="Bucket Name", required=True)
    aws_access_key_id = fields.Char(string="AWS Access Key ID", required=True)
    aws_secret_access_key = fields.Char(string="AWS Secret Access Key", required=True)
    region_name = fields.Char(string="Region", default="us-east-1")
    cdn_url = fields.Char(string="CND Url")

    def test_connection(self):
        try:
            import certifi
            verify = certifi.where()
        except Exception:
            verify = True
        for record in self:
            try:
                s3 = boto3.client(
                    's3',
                    aws_access_key_id=record.aws_access_key_id,
                    aws_secret_access_key=record.aws_secret_access_key,
                    region_name=record.region_name,
                    verify=verify,
                )
                s3.list_buckets()
                message = _('Connection successful!')
                notif_type = 'success'
            except ClientError as e:
                message = _('Connection failed: %s') % e
                notif_type = 'danger'
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('S3 Connection Test'),
                    'message': message,
                    'type': notif_type,
                    'sticky': False,
                }
            }

    def _get_s3_client(self):
        """Initialize a boto3 S3 client"""
        try:
            import certifi
            verify = certifi.where()
        except Exception:
            verify = True
        return boto3.client(
            's3',
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            region_name=self.region_name,
            verify=verify,
        )

    def upload_file(self, file_name, object_name=None):
        """Upload a file to S3"""
        # S3_CDN_URL = self.env['ir.config_parameter'].sudo().get_param('s3_cdn_url')
        self.ensure_one()
        s3 = self._get_s3_client()
        bucket = self.name
        object_name = object_name or file_name

        try:
            with open(file_name, "rb") as f:
                s3.upload_fileobj(f, bucket, object_name)
        except ClientError as e:
            raise UserError(_("Failed to upload file to S3: %s") % e)
        # s3_url = f"https://{bucket}.s3.{self.region_name}.amazonaws.com/{object_name}"
        s3_url = f"{self.cdn_url}/{object_name}"
        return s3_url


    def download_file(self, object_name, destination):
        """Download a file from S3"""
        self.ensure_one()
        s3 = self._get_s3_client()
        bucket = self.name
        try:
            with open(destination, "wb") as f:
                s3.download_fileobj(bucket, object_name, f)
        except ClientError as e:
            raise UserError(_("Failed to download file from S3: %s") % e)
        return True

    def list_files(self, prefix=None):
        """List all files in a bucket"""
        self.ensure_one()
        s3 = self._get_s3_client()
        bucket = self.name
        try:
            response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix or "")
            return [obj['Key'] for obj in response.get('Contents', [])]
        except ClientError as e:
            raise UserError(_("Failed to list files: %s") % e)


    def upload_base64(self, b64_data, object_name):
        """Upload Base64 data to S3"""
        # S3_CDN_URL = self.env['ir.config_parameter'].sudo().get_param('s3_cdn_url')
        self.ensure_one()
        s3 = self._get_s3_client()
        bucket = self.name

        try:
            binary_data = base64.b64decode(b64_data)
            s3.put_object(Bucket=bucket, Key=object_name, Body=binary_data)
        except ClientError as e:
            raise UserError(_("Failed to upload Base64 data to S3: %s") % e)
        # s3_url = f"https://{bucket}.s3.{self.region_name}.amazonaws.com/{object_name}"
        s3_url = f"{self.cdn_url}/{object_name}"
        return s3_url

    def detect_content_type(self, content):
        """Detect whether content is a file path, URL, file object, or Base64 string."""
        if isinstance(content, str):
            if os.path.exists(content) and os.path.isfile(content):
                return 'file_path'
            parsed = urlparse(content)
            if parsed.scheme in ('http', 'https') and parsed.netloc:
                return 'url'
            base64_pattern = r'^[A-Za-z0-9+/=]+\Z'
            stripped = content.strip().replace('\n', '').replace(' ', '')
            if len(stripped) % 4 == 0 and re.match(base64_pattern, stripped):
                try:
                    base64.b64decode(stripped, validate=True)
                    return 'base64'
                except Exception:
                    pass
        if isinstance(content, io.IOBase):
            return 'file_object'
        if isinstance(content, bytes):
            return 'bytes'
        return 'unknown'

    def upload_to_s3(self, content, object_name):
        """
        Upload content to S3. Content can be:
        - local file path
        - URL
        - file object (BytesIO)
        - Base64 string
        Returns the S3 public URL.
        """
        # S3_CDN_URL = self.env['ir.config_parameter'].sudo().get_param('s3_cdn_url')
        # 1️⃣ Detect type
        content_type = self.detect_content_type(content)

        # 2️⃣ Read bytes based on type
        if content_type == 'file_path':
            with open(content, 'rb') as f:
                file_bytes = f.read()
            file_name = os.path.basename(content)
        elif content_type == 'url':
            response = requests.get(content)
            response.raise_for_status()
            file_bytes = response.content
            file_name = os.path.basename(urlparse(content).path)
        elif content_type == 'file_object':
            file_bytes = content.read()
            file_name = getattr(content, 'name', 'file_from_object')
        elif content_type == 'base64':
            file_bytes = base64.b64decode(content)
            file_name = object_name.split('/')[-1]
        elif content_type == 'bytes':
            file_bytes = base64.b64decode(content)
            file_name = object_name.split('/')[-1]
        else:
            raise ValueError("Unsupported content type")
        # 3️⃣ Determine MIME type
        mimetype, _ = mimetypes.guess_type(file_name)
        if not mimetype:
            mimetype = 'application/octet-stream'

        self.ensure_one()
        s3 = self._get_s3_client()
        bucket = self.name

        s3.put_object(
            Bucket=bucket,
            Key=object_name,
            Body=file_bytes,
            ContentType=mimetype,
            ACL='public-read'
        )
        # s3_url = f"https://{bucket}.s3.{self.region_name}.amazonaws.com/{object_name}"
        s3_url = f"{self.cdn_url}/{object_name}"
        return s3_url
