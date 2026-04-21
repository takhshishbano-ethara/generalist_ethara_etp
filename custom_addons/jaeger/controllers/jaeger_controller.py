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

        filename = os.path.basename(file_path)

        if os.path.isfile(file_path):
            with open(file_path, "rb") as f:
                content = f.read()
        else:
            content = repo._download_from_s3_bytes(file_path)
            if content is None:
                return request.not_found()

        return request.make_response(
            content,
            headers=[
                ("Content-Type", "application/x-ndjson"),
                ("Content-Disposition", content_disposition(filename)),
            ],
        )

    @http.route("/jaeger/webhook/trajectory", type="json", auth="public", csrf=False)
    def trajectory_webhook(self, **kwargs):
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
