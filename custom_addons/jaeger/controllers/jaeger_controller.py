import hmac
import logging
import os

from odoo import fields, http
from odoo.http import request, content_disposition

_logger = logging.getLogger(__name__)

class JaegerController(http.Controller):

    @http.route("/jaeger/download/<int:repo_id>/<string:filetype>",
                type="http", auth="user")
    def download_jsonl(self, repo_id, filetype, **kwargs):
        repo = request.env["jaeger.repository"].browse(repo_id)
        if not repo.exists():
            return request.not_found()

        content = self._download_from_s3_by_type(repo, filetype)
        if content is None:
            return request.not_found()

        name_map = {
            "raw_dataset": f"{repo.org}__{repo.repo_name}_raw_dataset.jsonl",
            "prs": f"{repo.org}__{repo.repo_name}_prs.jsonl",
            "filtered_prs": f"{repo.org}__{repo.repo_name}_filtered_prs.jsonl",
        }
        filename = name_map.get(filetype, f"{filetype}.jsonl")

        return request.make_response(
            content,
            headers=[
                ("Content-Type", "application/x-ndjson"),
                ("Content-Disposition", content_disposition(filename)),
            ],
        )

    @staticmethod
    def _download_from_s3_by_type(repo, filetype):
        name_map = {
            "raw_dataset": f"{repo.org}__{repo.repo_name}_raw_dataset.jsonl",
            "prs": f"{repo.org}__{repo.repo_name}_prs.jsonl",
            "filtered_prs": f"{repo.org}__{repo.repo_name}_filtered_prs.jsonl",
        }
        filename = name_map.get(filetype)
        if not filename:
            return None
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            return None

        s3_bucket = os.environ.get("JAEGER_S3_BUCKET", "")
        s3_region = os.environ.get("JAEGER_S3_REGION", "ap-south-1")
        s3_prefix = os.environ.get("JAEGER_S3_PREFIX", "jaeger/phase1")
        if not s3_bucket:
            return None

        mode = repo.pipeline_mode or "swe"
        s3_key = f"{s3_prefix}/{mode}/{repo.id}/{filename}"
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
            _logger.debug("S3 download by type failed for %s", s3_key, exc_info=True)
            return None

    @http.route("/jaeger/webhook/trajectory", type="json", auth="none", csrf=False)
    def trajectory_webhook(self, **kwargs):
        from odoo.addons.jaeger.models.credential_manager import get_encrypted_param
        expected = get_encrypted_param(request.env, "jaeger.webhook_secret")
        token = request.httprequest.headers.get("X-Jaeger-Token", "")
        if not expected or token != expected:
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

    # ── Pipeline webhook (kaiju pattern) ─────────────────────────────────

    _webhook_auth_warned = False

    def _verify_pipeline_token(self):
        secret = os.environ.get("JAEGER_WEBHOOK_TOKEN", "")
        if not secret:
            if not JaegerController._webhook_auth_warned:
                _logger.warning(
                    "JAEGER_WEBHOOK_TOKEN not set — pipeline webhook auth is DISABLED. "
                    "Set this env var in production to secure the webhook endpoint."
                )
                JaegerController._webhook_auth_warned = True
            return True
        token = request.httprequest.headers.get("X-Jaeger-Token", "")
        return hmac.compare_digest(token, secret)

    @http.route(
        "/jaeger/webhook/pipeline", type="jsonrpc", auth="none",
        methods=["POST"], csrf=False,
    )
    def pipeline_webhook(self, **kwargs):
        if not self._verify_pipeline_token():
            return {"error": "unauthorized"}

        repo_id = kwargs.get("repo_id")
        if not repo_id:
            return {"error": "missing repo_id"}

        repo = (
            request.env["jaeger.repository"]
            .sudo()
            .search([("id", "=", int(repo_id))], limit=1)
        )
        if not repo:
            return {"error": "repo not found"}

        msg_type = kwargs.get("type", "")

        if msg_type == "heartbeat":
            repo.write({"last_heartbeat": fields.Datetime.now()})
            return {"ok": True}

        if msg_type == "progress":
            vals = {
                "pr_collection_status": "running",
                "last_heartbeat": fields.Datetime.now(),
            }
            step = kwargs.get("step")
            message = kwargs.get("message", "")
            if step:
                vals["pr_collection_step"] = "Step %s: %s" % (step, message)
            progress = kwargs.get("progress")
            if progress is not None:
                vals["pr_collection_progress"] = float(progress)
            if message:
                repo._append_log(message)
            repo.write(vals)
            return {"ok": True}

        if msg_type == "status":
            status = kwargs.get("status")
            if status == "done":
                s3_paths = kwargs.get("s3_paths", {})
                counts = kwargs.get("counts", {})
                repo.write({
                    "pr_collection_progress": 100,
                    "pr_collection_step": "Creating instances from S3...",
                    "error_message": False,
                    "terminal_state": "none",
                    "total_prs_fetched": counts.get("total_prs", 0),
                    "filtered_prs_count": counts.get("filtered_prs", 0),
                    "issues_fetched_count": counts.get("issues", 0),
                    "raw_dataset_count": counts.get("raw_dataset", 0),
                })
                try:
                    repo._create_instances_from_s3(s3_paths)
                except Exception as e:
                    _logger.exception(
                        "Instance creation from S3 failed for repo %s", repo_id,
                    )
                    repo.write({
                        "pr_collection_status": "failed",
                        "error_message": "Instance creation failed: %s" % str(e)[:1500],
                    })
                    return {"ok": True}

                repo.write({
                    "pr_collection_status": "done",
                    "pr_collection_step": "",
                })

                if repo.pipeline_mode == "lht" and not repo.task_category:
                    repo.write({"task_category": "long_horizon"})
                elif repo.pipeline_mode == "rct" and not repo.task_category:
                    repo.write({"task_category": "real_coder"})

                try:
                    gate_ok, _ = repo._check_current_gate()
                    if gate_ok:
                        next_stage = repo._next_stage()
                        if next_stage:
                            repo.write({"current_stage": next_stage})
                except Exception:
                    _logger.warning(
                        "Stage advance failed for repo %s (cron will catch up)",
                        repo_id, exc_info=True,
                    )
                return {"ok": True}

            elif status == "failed":
                error = kwargs.get("error", "Unknown error")
                repo.write({
                    "pr_collection_status": "failed",
                    "error_message": str(error)[:2000],
                    "pr_collection_step": "",
                })
                return {"ok": True}

        return {"error": "unknown message type"}
