import logging
import os

from odoo import http
from odoo.http import request, content_disposition

_logger = logging.getLogger(__name__)


class JaegerController(http.Controller):

    @http.route("/jaeger/download/<int:repo_id>/<string:filetype>",
                type="http", auth="user")
    def download_jsonl(self, repo_id, filetype, **kwargs):
        repo = request.env["jaeger.repository"].browse(repo_id)
        if not repo.exists():
            return request.not_found()

        path_map = {
            "raw_dataset": repo.raw_dataset_jsonl_path,
            "prs": repo.prs_jsonl_path,
            "filtered_prs": repo.filtered_prs_jsonl_path,
        }
        file_path = path_map.get(filetype)
        if not file_path:
            return request.not_found()

        ICP = request.env["ir.config_parameter"].sudo()
        allowed_base = ICP.get_param("jaeger.output_dir", "/tmp/jaeger_data")
        real_path = os.path.realpath(file_path)
        if not real_path.startswith(os.path.realpath(allowed_base)):
            return request.not_found()

        filename = os.path.basename(file_path)

        if os.path.isfile(file_path):
            with open(file_path, "rb") as f:
                content = f.read()
        else:
            content = self._download_from_s3(repo, file_path)
            if content is None:
                return request.not_found()

        return request.make_response(
            content,
            headers=[
                ("Content-Type", "application/x-ndjson"),
                ("Content-Disposition", content_disposition(filename)),
            ],
        )

    @staticmethod
    def _download_from_s3(repo, file_path):
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            return None

        ICP = request.env["ir.config_parameter"].sudo()
        s3_bucket = ICP.get_param("jaeger.s3_bucket", "")
        s3_region = ICP.get_param("jaeger.s3_region", "ap-south-1")
        s3_prefix = ICP.get_param("jaeger.s3_prefix", "jaeger/phase1")
        if not s3_bucket:
            return None

        filename = os.path.basename(file_path)
        s3_key = f"{s3_prefix}/{repo.id}/{filename}"
        try:
            config_kwargs = {"connect_timeout": 10, "read_timeout": 60}
            if os.environ.get("JAEGER_S3_ENDPOINT"):
                config_kwargs["s3"] = {"addressing_style": "path"}
            client = boto3.client(
                "s3",
                region_name=s3_region,
                endpoint_url=os.environ.get(
                    "JAEGER_S3_ENDPOINT",
                    f"https://s3.{s3_region}.amazonaws.com",
                ),
                config=BotoConfig(**config_kwargs),
            )
            resp = client.get_object(Bucket=s3_bucket, Key=s3_key)
            return resp["Body"].read()
        except Exception:
            _logger.debug("S3 download fallback failed for %s", s3_key, exc_info=True)
            return None

    @http.route("/jaeger/webhook/trajectory", type="json", auth="public", csrf=False)
    def trajectory_webhook(self, **kwargs):
        expected = request.env["ir.config_parameter"].sudo().get_param("jaeger.webhook_secret")
        if not expected or kwargs.get("secret") != expected:
            return {"error": "unauthorized"}

        job_id = kwargs.get("job_id")
        status = kwargs.get("status")
        results = kwargs.get("results", {})

        if not job_id:
            return {"error": "job_id required"}

        repo = (
            request.env["jaeger.repository"]
            .sudo()
            .search([("eks_job_id", "=", job_id)], limit=1)
        )
        if not repo:
            _logger.warning("Trajectory webhook: unknown job_id %s", job_id)
            return {"error": "unknown job_id"}

        repo._handle_trajectory_webhook(status, results)
        return {"status": "ok"}
