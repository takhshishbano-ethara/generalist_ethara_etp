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
        if not expected:
            return {"error": "unauthorized — no webhook secret configured"}
        token = request.httprequest.headers.get("X-Jaeger-Token", "")
        if not hmac.compare_digest(token, expected):
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
            ICP = request.env["ir.config_parameter"].sudo()
            secret = ICP.get_param("jaeger.pipeline_webhook_token", "")
        if not secret:
            sandbox = request.env["ir.config_parameter"].sudo().get_param(
                "jaeger.sandbox_mode", "0",
            ) == "1"
            if sandbox:
                if not JaegerController._webhook_auth_warned:
                    _logger.warning(
                        "JAEGER_WEBHOOK_TOKEN not set — pipeline webhook auth is DISABLED (sandbox mode). "
                        "Set this env var in production to secure the webhook endpoint."
                    )
                    JaegerController._webhook_auth_warned = True
                return True
            if not JaegerController._webhook_auth_warned:
                _logger.error(
                    "JAEGER_WEBHOOK_TOKEN not set and sandbox_mode is OFF — "
                    "rejecting pipeline webhook requests. "
                    "Set JAEGER_WEBHOOK_TOKEN env var or jaeger.pipeline_webhook_token ICP."
                )
                JaegerController._webhook_auth_warned = True
            return False
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

        # ── Stage 3: Docker Build webhooks ───────────────────────────────

        if msg_type == "build_progress":
            instance_id = kwargs.get("instance_id")
            build_status = kwargs.get("status", "")
            image_name = kwargs.get("image_name", "")
            log_tail = kwargs.get("log_tail", "")
            if instance_id:
                inst = request.env["jaeger.instance"].sudo().browse(int(instance_id))
                if inst.exists():
                    vals = {"docker_build_status": build_status}
                    if image_name:
                        vals["docker_image_name"] = image_name
                    if log_tail:
                        vals["docker_build_log"] = log_tail
                    inst.write(vals)
            repo.write({"last_heartbeat": fields.Datetime.now()})
            return {"ok": True}

        if msg_type == "build_base_done":
            base_name = kwargs.get("base_image_name", "")
            base_status = kwargs.get("base_image_status", "built")
            repo.write({
                "base_image_name": base_name,
                "base_image_status": base_status,
                "last_heartbeat": fields.Datetime.now(),
            })
            repo._append_log(f"Base image {base_status}: {base_name}")
            return {"ok": True}

        if msg_type == "build_done":
            built = kwargs.get("images_built_count", 0)
            failed = kwargs.get("images_failed_count", 0)
            vals = {
                "docker_build_status": "done",
                "docker_build_progress": 100.0,
                "images_built_count": built,
                "images_failed_count": failed,
                "error_message": False,
                "terminal_state": "none",
            }
            if built == 0 and failed > 0:
                vals["docker_build_status"] = "failed"
                vals["terminal_state"] = "build_failed"
                vals["error_message"] = "All %d image builds failed" % failed
            else:
                # Gate-check before advancing stage (consistent with stage2 webhook)
                repo.write(vals)
                try:
                    gate_ok, _ = repo._check_current_gate()
                    if gate_ok:
                        repo.write({
                            "current_stage": "stage4",
                            "test_execution_status": "running",
                        })
                except Exception:
                    _logger.warning(
                        "Stage advance after build_done failed for repo %s (cron will catch up)",
                        repo_id, exc_info=True,
                    )
                repo._append_log(f"Docker build complete: {built} built, {failed} failed")
                return {"ok": True}
            repo.write(vals)
            repo._append_log(f"Docker build complete: {built} built, {failed} failed")
            return {"ok": True}

        if msg_type == "build_failed":
            error = kwargs.get("error", "Unknown error")
            repo.write({
                "docker_build_status": "failed",
                "error_message": str(error)[:2000],
            })
            return {"ok": True}

        # ── Stage 4: Test Execution webhooks ─────────────────────────────

        if msg_type == "test_progress":
            instance_id = kwargs.get("instance_id")
            is_valid = kwargs.get("is_valid", False)
            summary = kwargs.get("summary", "")
            if instance_id:
                inst = request.env["jaeger.instance"].sudo().browse(int(instance_id))
                if inst.exists():
                    import json as _json
                    vals = {
                        "run_log": (kwargs.get("run_log") or "")[-50000:],
                        "test_patch_run_log": (kwargs.get("test_patch_log") or "")[-50000:],
                        "fix_patch_run_log": (kwargs.get("fix_patch_log") or "")[-50000:],
                        "is_valid": is_valid,
                        "validation_error": "" if is_valid else summary,
                    }
                    run_result = kwargs.get("run_result")
                    if run_result:
                        vals["run_result_json"] = _json.dumps(run_result)
                        vals["run_passed_count"] = run_result.get("passed_count", 0)
                        vals["run_failed_count"] = run_result.get("failed_count", 0)
                    test_result = kwargs.get("test_result")
                    if test_result:
                        vals["test_patch_result_json"] = _json.dumps(test_result)
                        vals["test_patch_passed_count"] = test_result.get("passed_count", 0)
                        vals["test_patch_failed_count"] = test_result.get("failed_count", 0)
                    fix_result = kwargs.get("fix_result")
                    if fix_result:
                        vals["fix_patch_result_json"] = _json.dumps(fix_result)
                        vals["fix_patch_passed_count"] = fix_result.get("passed_count", 0)
                        vals["fix_patch_failed_count"] = fix_result.get("failed_count", 0)
                    inst.write(vals)
            repo.write({"last_heartbeat": fields.Datetime.now()})
            return {"ok": True}

        if msg_type == "test_done":
            valid = kwargs.get("valid_count", 0)
            invalid = kwargs.get("invalid_count", 0)
            errors = kwargs.get("error_count", 0)
            vals = {
                "test_execution_status": "done",
                "test_execution_progress": 100.0,
                "instances_valid_count": valid,
                "instances_invalid_count": invalid,
                "instances_error_count": errors,
                "instances_tested_count": valid + invalid + errors,
                "error_message": False,
                "terminal_state": "none",
                "cancel_requested": False,
            }
            if valid == 0 and (invalid + errors) > 0:
                vals["terminal_state"] = "no_valid_instances"
                vals["error_message"] = "All %d tested instances are invalid" % (invalid + errors)
            repo.write(vals)
            if valid > 0:
                try:
                    gate_ok, _ = repo._check_current_gate()
                    if gate_ok:
                        repo.write({"current_stage": "stage5"})
                except Exception:
                    _logger.warning(
                        "Stage advance after test_done failed for repo %s (cron will catch up)",
                        repo_id, exc_info=True,
                    )
            repo._append_log(f"Test execution complete: {valid} valid, {invalid} invalid, {errors} errors")
            return {"ok": True}

        if msg_type == "test_failed":
            error = kwargs.get("error", "Unknown error")
            repo.write({
                "test_execution_status": "failed",
                "error_message": str(error)[:2000],
                "cancel_requested": False,
            })
            return {"ok": True}

        return {"error": "unknown message type"}
