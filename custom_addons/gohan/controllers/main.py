"""Gohan HTTP controllers — webhook endpoint + API."""

import hashlib
import hmac
import json
import logging
import os

import psycopg2

from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

# Shared advisory-lock namespace. Re-exported from gohan_job so the webhook
# handler and the S3 poller / reconcile cron in models/gohan_job.py agree on
# the same lock id and serialize writes to a single job row.
from ..models.gohan_job import _GOHAN_WEBHOOK_LOCK_NS  # noqa: E402

# Header names the Lambda may send the shared token under. The first is the
# spec-aligned Gohan name; the others are accepted for backwards compatibility
# with the previous Vegeta/Leviathan deployments so an existing Lambda
# function does NOT have to be redeployed when Odoo is upgraded from Vegeta
# to Gohan. New deployments should use ``X-Gohan-Token``.
_ACCEPTED_TOKEN_HEADERS = (
    "X-Gohan-Token",
    "X-Vegeta-Token",
    "X-Leviathan-Token",
)


def _get_webhook_token_candidates() -> list[str]:
    """Return the ordered list of valid shared-secret values.

    Looks up the canonical Odoo system parameter first, then falls back to
    the legacy OS environment variables. Returning every non-empty candidate
    (rather than only the first match) lets operators rotate the token
    incrementally: the new value can be staged in
    ``gohan.webhook_token`` before the Lambda environment is updated, and
    the old env-var value keeps working until the rotation is complete.
    """
    candidates: list[str] = []
    try:
        sys_param = (
            request.env["ir.config_parameter"]
            .sudo()
            .get_param("gohan.webhook_token")
        )
    except Exception:
        # Pre-init / cursor-less edge cases — degrade silently to env-var path.
        sys_param = None
    if sys_param:
        candidates.append(sys_param)
    for env_name in ("GOHAN_WEBHOOK_TOKEN", "VEGETA_WEBHOOK_TOKEN", "LEVIATHAN_WEBHOOK_TOKEN"):
        env_val = os.environ.get(env_name)
        if env_val:
            candidates.append(env_val)
    return candidates


def _verify_webhook_token():
    """Verify the shared-secret token sent by the extraction Lambda.

    Accepts the token under any of ``X-Gohan-Token`` (current),
    ``X-Vegeta-Token`` or ``X-Leviathan-Token`` (legacy). Matches against
    the ``gohan.webhook_token`` system parameter first, then falls back to
    the OS environment variables ``GOHAN_WEBHOOK_TOKEN``,
    ``VEGETA_WEBHOOK_TOKEN`` and ``LEVIATHAN_WEBHOOK_TOKEN``. All
    comparisons are constant-time. Returns False (fail-closed) when no
    secret is configured at all.

    This is the legacy direct-Lambda path. The newer API-Gateway path uses
    HMAC signatures (see ``_verify_hmac_signature``) instead of a shared
    token.
    """
    candidates = _get_webhook_token_candidates()
    if not candidates:
        _logger.error(
            "[gohan][webhook] no shared secret configured -- rejecting "
            "legacy webhook. Set gohan.webhook_token under Settings -> "
            "Gohan -> API Gateway, or export GOHAN_WEBHOOK_TOKEN / "
            "VEGETA_WEBHOOK_TOKEN in the Odoo process environment."
        )
        return False

    provided_tokens = [
        request.httprequest.headers.get(h) for h in _ACCEPTED_TOKEN_HEADERS
    ]
    provided_tokens = [t for t in provided_tokens if t]
    if not provided_tokens:
        return False

    for provided in provided_tokens:
        for secret in candidates:
            if hmac.compare_digest(provided, secret):
                return True
    return False


def _verify_hmac_signature(raw_body: bytes) -> bool:
    """Verify the X-Gohan-Signature HMAC-SHA256 header against ``gohan.hmac_secret``.

    Constant-time comparison via :func:`hmac.compare_digest` to avoid
    timing attacks. The shared secret comes from the system parameter,
    NOT the OS environment, so it can be rotated without redeploying
    Odoo. If the secret is unset, the webhook is rejected.
    """
    secret = (
        request.env["ir.config_parameter"]
        .sudo()
        .get_param("gohan.hmac_secret")
    )
    if not secret:
        _logger.error(
            "[gohan][webhook] gohan.hmac_secret not configured -- rejecting "
            "spec webhook. Set it under Settings -> Gohan -> API Gateway."
        )
        return False
    provided = request.httprequest.headers.get("X-Gohan-Signature", "") or ""
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


class GohanController(http.Controller):

    @http.route(
        "/api/v1/gohan/webhook/extraction-complete",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def webhook_extraction_complete(self, **kwargs):
        """Webhook called by the external extraction service when a job finishes."""
        req = request.httprequest
        body_len = len(req.data or b"")
        _logger.info(
            "[gohan][diag][webhook] HIT extraction-complete: remote=%s "
            "method=%s ua=%r token_present=%s body_bytes=%d",
            req.remote_addr,
            req.method,
            req.headers.get("User-Agent", ""),
            bool(req.headers.get("X-Gohan-Token")),
            body_len,
        )
        if not _verify_webhook_token():
            _logger.warning(
                "[gohan][diag][webhook] AUTH FAILED — token_configured=%s "
                "token_header_set=%s. Set gohan.webhook_token in Settings -> "
                "Gohan -> API Gateway AND ensure the Lambda sends the same "
                "value as X-Gohan-Token.",
                bool(
                    request.env["ir.config_parameter"]
                    .sudo()
                    .get_param("gohan.webhook_token")
                ),
                bool(req.headers.get("X-Gohan-Token")),
            )
            return Response(
                json.dumps({"error": "Unauthorized"}),
                status=401,
                content_type="application/json",
            )

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, ValueError):
            _logger.warning(
                "[gohan][diag][webhook] invalid JSON body (first 300 bytes): %r",
                (request.httprequest.data or b"")[:300],
            )
            return Response(
                json.dumps({"error": "Invalid JSON body"}),
                status=400,
                content_type="application/json",
            )

        job_id = data.get("job_id")
        _logger.info(
            "[gohan][diag][webhook] parsed: job_id=%s status=%s success=%s keys=%s",
            job_id, data.get("status"), data.get("success"), list(data.keys()),
        )
        if not job_id:
            return Response(
                json.dumps({"error": "Missing job_id"}),
                status=400,
                content_type="application/json",
            )

        record = request.env["gohan.job"].sudo().browse(int(job_id))
        if not record.exists():
            return Response(
                json.dumps({"error": f"Job {job_id} not found"}),
                status=404,
                content_type="application/json",
            )

        # From here on the controller emits pipeline-log events. We capture the
        # db name + record id at the top because _append_pipeline_event opens
        # its OWN cursor (so the log entry survives even if this request
        # later rolls back -- which is exactly what we want for error rows).
        db_name = request.env.cr.dbname
        record_id = record.id
        record._append_pipeline_event(
            db_name, record_id, "webhook.callback", "received",
            message=f"Lambda callback received (status={data.get('status')!r}, "
                    f"success={data.get('success')!r})",
            payload_bytes=body_len,
            payload_keys=sorted([str(k) for k in data.keys()])[:20],
        )

        # CRITICAL FIX (retry storm): use a Postgres advisory lock instead
        # of SELECT FOR UPDATE. Under Odoo's REPEATABLE READ isolation,
        # FOR UPDATE on a row already modified after this transaction's
        # snapshot raises ``could not serialize access due to concurrent
        # update`` (SQLSTATE 40001), which the generic except below turned
        # into a 500 -- which made the Lambda's _send_callback retry,
        # cascading into a 30+ delivery storm.
        #
        # pg_try_advisory_xact_lock is NON-blocking and snapshot-independent:
        # the first webhook for a given job_id wins the lock; any concurrent
        # duplicate returns False immediately and we 200 idempotent. The
        # lock auto-releases at transaction end (commit OR rollback).
        # The lock is shared with the S3 poller / reconcile cron. If the
        # cron is mid-tick on this job_id at the exact moment a Lambda
        # success callback arrives, a single try would lose the callback
        # forever (Lambda doesn't retry under our max_attempts=1 config),
        # leaving the job stuck in 'extracting' until the next cron tick.
        # So we retry the lock for up to ~3s. The cron's per-job critical
        # section is sub-second in the happy path, so 6 short tries are
        # plenty. If we still can't get it, it's a real concurrent
        # webhook duplicate (advisory lock holder is a peer webhook, not
        # a cron), so dropping is correct.
        import time as _time
        locked = False
        for _attempt in range(6):
            request.env.cr.execute(
                "SELECT pg_try_advisory_xact_lock(%s, %s)",
                (_GOHAN_WEBHOOK_LOCK_NS, record.id),
            )
            if request.env.cr.fetchone()[0]:
                locked = True
                break
            _time.sleep(0.5)
        if not locked:
            record._append_pipeline_event(
                db_name, record_id, "webhook.callback", "ignored",
                message="Advisory lock held by a peer for ~3s; dropping "
                        "this delivery and queueing an auto-rescue "
                        "(reads raw_data.json from S3 and advances state "
                        "if the peer didn't already). Job will NOT be "
                        "left stuck.",
            )
            # AUTO-RESCUE FALLBACK: if the peer was a cron that didn't
            # actually advance state (e.g. it ran BEFORE the Lambda's
            # raw_data.json upload finished), schedule a background
            # _attempt_s3_rescue 8s from now. By then the peer is done,
            # raw_data.json is in S3, and the rescue will populate
            # screenshot_keys / asset_keys / site_discovery_json /
            # prd_prompt -- the same fields leviathan auto-populates
            # via its webhook -- so the form view's Asset Previews
            # tab lights up exactly like leviathan's.
            def _deferred_rescue():
                from ..models.gohan_job import _submit_bg
                _submit_bg(
                    f"webhook-drop-rescue[job={record_id}]",
                    record._delayed_s3_rescue, db_name, record_id, 8.0,
                )
            request.env.cr.postcommit.add(_deferred_rescue)
            return Response(
                json.dumps({
                    "ignored": True,
                    "reason": "Lock contention (peer held for ~3s); "
                              "auto-rescue queued",
                }),
                status=200,
                content_type="application/json",
            )
        # Lock acquired -- we are the only writer for this job. Drop the
        # stale snapshot read of `state` so the idempotency guard below
        # sees the latest committed value.
        record.invalidate_recordset(["state"])

        # Lightweight "extraction started" ping — the Lambda sends this when it
        # actually picks the job up. Update last_heartbeat so the watchdog
        # measures real progress, not time-since-state-change (a job merely
        # queued in AWS shouldn't be killed for being slow to start).
        if data.get("status") == "started":
            # Same self-serialization fix as the success path: use a fresh
            # cursor so the prior cr_log commit ("webhook.callback received")
            # doesn't make the request cursor's snapshot stale and trip 40001.
            heartbeat_updated = False
            from odoo.modules.registry import Registry as _Registry
            from odoo import api as _api, SUPERUSER_ID as _SU
            with _Registry(db_name).cursor() as cr_hb:
                env_hb = _api.Environment(cr_hb, _SU, {})
                rec_hb = env_hb["gohan.job"].browse(record_id)
                if rec_hb.exists() and rec_hb.state == "extracting":
                    rec_hb.write({"last_heartbeat": fields.Datetime.now()})
                    cr_hb.commit()
                    heartbeat_updated = True
                    _logger.info(
                        "[gohan][job=%s] extraction started ping",
                        rec_hb.name,
                    )
            record._append_pipeline_event(
                db_name, record_id, "extraction.lambda_started", "ok",
                message="Lambda picked up the job and started extracting",
                heartbeat_updated=heartbeat_updated,
            )
            return Response(
                json.dumps({"status": "ack"}),
                status=200,
                content_type="application/json",
            )

        if record.state != "extracting":
            _logger.info(
                "Webhook for job %s ignored: state is '%s' "
                "(expected 'extracting') -- idempotency guard",
                job_id,
                record.state,
            )
            # Record the no-op so operators can see WHY a duplicate Lambda
            # retry was dropped (state machine + idempotency are not always
            # obvious from the UI alone).
            record._append_pipeline_event(
                db_name, record_id, "webhook.callback", "ignored",
                message=f"Idempotency guard: job already in state '{record.state}' "
                        "(callback dropped -- not a failure)",
                current_state=record.state,
            )
            return Response(
                json.dumps({
                    "ignored": True,
                    "reason": f"Job in state '{record.state}' (already processed)",
                }),
                status=200,
                content_type="application/json",
            )

        if record.cancel_requested:
            _logger.info(
                "Webhook for job %s ignored: cancel_requested=True", job_id
            )
            record._append_pipeline_event(
                db_name, record_id, "webhook.callback", "cancelled",
                message="Operator-requested cancel arrived before Lambda finished; "
                        "callback ignored.",
            )
            return Response(
                json.dumps({"ignored": True, "reason": "Job was cancelled"}),
                status=200,
                content_type="application/json",
            )

        try:
            success = data.get("success")
            if not success:
                error_msg = data.get("error", "Extraction failed (no details)")
                record._append_pipeline_event(
                    db_name, record_id, "extraction.callback", "error",
                    message=f"Lambda reported failure: {error_msg}",
                )
                # Same self-serialization fix: run _mark_failed on a fresh
                # cursor so the prior cr_log commits don't make the request
                # cursor's snapshot stale.
                from odoo.modules.registry import Registry as _Registry
                from odoo import api as _api, SUPERUSER_ID as _SU
                with _Registry(db_name).cursor() as cr_fail:
                    env_fail = _api.Environment(cr_fail, _SU, {})
                    rec_fail = env_fail["gohan.job"].browse(record_id)
                    if rec_fail.exists() and rec_fail.state == "extracting":
                        rec_fail._mark_failed(error_msg)
                        cr_fail.commit()
                return Response(
                    json.dumps({"status": "failed"}),
                    status=200,
                    content_type="application/json",
                )

            site_discovery = data.get("site_discovery")
            prd_prompt = data.get("prd_prompt")
            artifacts = data.get("artifacts")
            screenshot_keys = data.get("screenshot_keys", [])
            asset_keys = data.get("asset_keys", [])
            is_partial = data.get("partial", False)
            warnings = data.get("warnings") or []

            ICP = request.env["ir.config_parameter"].sudo()
            s3_bucket = ICP.get_param("gohan.s3_bucket")
            s3_key_id = ICP.get_param("gohan.s3_access_key_id")
            s3_secret = ICP.get_param("gohan.s3_secret_access_key")
            s3_region = ICP.get_param("gohan.s3_region") or "us-east-1"
            s3_folder = ICP.get_param("gohan.s3_folder") or "gohan"
            cdn_url = ICP.get_param("gohan.s3_cdn_url")

            # PARITY WITH LEVIATHAN (production): defer the multi-MB artifacts
            # upload to a background thread so the webhook returns in <50 ms.
            # Doing the upload synchronously inside the request is what caused
            # the retry storm -- the Lambda's _send_callback has a 60s httpx
            # timeout, and a slow webhook trips it, the Lambda retries, each
            # retry races on the same row, Postgres returns 40001, the
            # controller returned 500, Lambda retried again, etc.
            #
            # The artifacts_url is computed from the {bucket, folder, job_name}
            # tuple deterministically -- it is the SAME URL whether we upload
            # now or in the background -- so we can write it on the record
            # immediately and run the actual upload off the request.
            artifacts_url = None
            artifacts_pending = bool(artifacts and s3_bucket)
            s3_config = {
                "bucket": s3_bucket,
                "key_id": s3_key_id,
                "secret": s3_secret,
                "region": s3_region,
                "folder": s3_folder,
                "cdn_url": cdn_url,
            }
            if artifacts_pending:
                from ..services.s3_service import get_artifacts_folder_url
                artifacts_url = get_artifacts_folder_url(
                    job_name=record.name,
                    bucket=s3_bucket,
                    folder=s3_folder,
                    cdn_url=cdn_url,
                )

            write_vals = {
                "prd_prompt": prd_prompt,
                "last_heartbeat": fields.Datetime.now(),
            }

            if site_discovery:
                title = site_discovery.get("title")
                tech_stack = site_discovery.get("tech_stack")
                pages = site_discovery.get("pages")
                if title:
                    write_vals["site_name"] = title
                if tech_stack:
                    write_vals["tech_stack"] = json.dumps(tech_stack) if isinstance(tech_stack, dict) else str(tech_stack)
                if pages:
                    write_vals["page_count"] = len(pages)
                write_vals["site_discovery_json"] = site_discovery

            if artifacts_url:
                write_vals["artifacts_url"] = artifacts_url
            if screenshot_keys:
                write_vals["screenshot_keys"] = screenshot_keys
            if asset_keys:
                write_vals["asset_keys"] = asset_keys
            # Surface partial/warnings WITHOUT polluting error_message — a
            # successful-but-imperfect extraction is not a red failure. The
            # tasker sees a non-red "Partial extraction" banner instead.
            if warnings or is_partial:
                summary = warnings or ["Extraction was partial (deadline reached)."]
                write_vals["extraction_warnings"] = (
                    "\n".join(f"• {w}" for w in summary)[:1000]
                )
                _logger.info(
                    "[gohan][job=%s] extraction succeeded with warnings: %s",
                    record.name, summary,
                )
                record._append_pipeline_event(
                    db_name, record_id, "extraction.callback", "warn",
                    message="Lambda completed with partial data / warnings",
                    partial=is_partial,
                    warnings=list(summary)[:20],
                )
            else:
                write_vals["extraction_warnings"] = False

            # Full transparency: persist the complete Lambda callback. Artifacts
            # are trimmed to filenames — their content is large and already on S3.
            callback_snapshot = dict(data)
            if isinstance(callback_snapshot.get("artifacts"), dict):
                callback_snapshot["artifacts"] = sorted(callback_snapshot["artifacts"].keys())
            write_vals["lambda_callback_json"] = callback_snapshot

            # Backfill Run Metadata (spec) opportunistically. The Lambda may
            # include lambda_request_id / s3_artifact_prefix in its callback
            # body (the spec /gohan/webhook contract always does; the legacy
            # callback occasionally does too). Only overwrite when the
            # legacy invoke path didn't already populate the field.
            spec_request_id = (
                data.get("lambda_request_id")
                or data.get("request_id")
                or data.get("aws_request_id")
            )
            if spec_request_id and not record.lambda_request_id:
                write_vals["lambda_request_id"] = str(spec_request_id)[:120]
            spec_prefix = (
                data.get("s3_artifact_prefix")
                or data.get("s3_prefix")
                or data.get("artifacts_prefix")
            )
            if spec_prefix and not record.s3_artifact_prefix:
                write_vals["s3_artifact_prefix"] = str(spec_prefix)[:255]

            write_vals["state"] = "generating"

            # CRITICAL FIX (self-serialization-conflict): perform the main
            # row write on a FRESH cursor (its own short-lived transaction)
            # instead of the request cursor. Why this is necessary:
            #
            #   _append_pipeline_event opens its own cursor (cr_log) and
            #   commits writes to ``gohan_job.pipeline_log_json`` so log
            #   entries survive rollback. In the success path we call it
            #   multiple times BEFORE the main write:
            #     1. "webhook.callback received"    -> cr_log_1 commit at T1
            #     2. "extraction.callback warn"     -> cr_log_2 commit at T2
            #     3. record.write(write_vals)       -> request cursor at T0
            #
            #   The request cursor's REPEATABLE READ snapshot is older than
            #   T1/T2, so Postgres raises 40001 SerializationFailure on
            #   step 3 -- a SELF-conflict, not a real concurrent webhook.
            #   The caller then saw "Serialization conflict ... ignored"
            #   in the pipeline log and the row never advanced.
            #
            #   Doing the main write on cr_write (a fresh cursor) takes a
            #   new snapshot AFTER the pipeline_log commits, so it sees
            #   the latest committed state and the UPDATE never conflicts.
            #   The request-cursor advisory lock still serializes concurrent
            #   webhooks for the same job (acquired above), so we are still
            #   the only writer to this row.
            from odoo.modules.registry import Registry as _Registry
            from odoo import api as _api, SUPERUSER_ID as _SU
            with _Registry(db_name).cursor() as cr_write:
                env_write = _api.Environment(cr_write, _SU, {})
                rec_write = env_write["gohan.job"].browse(record_id)
                if not rec_write.exists():
                    raise RuntimeError(
                        f"gohan.job record {record_id} vanished during "
                        f"webhook processing"
                    )
                # Re-check state on the fresh snapshot. If a peer already
                # advanced it (which shouldn't happen because we hold the
                # advisory lock, but be defensive), treat as idempotent.
                if rec_write.state != "extracting":
                    _logger.info(
                        "[gohan][job=%s] cr_write: state already '%s' on "
                        "fresh snapshot -- skipping write (idempotent)",
                        rec_write.name, rec_write.state,
                    )
                else:
                    rec_write.write(write_vals)
                    cr_write.commit()

            record._append_pipeline_event(
                db_name, record_id, "extraction.callback", "ok",
                message="Extraction data persisted; transitioning to PRD generation",
                screenshots=len(screenshot_keys),
                assets=len(asset_keys),
                has_site_discovery=bool(site_discovery),
                has_prd_prompt=bool(prd_prompt),
                next_state="generating",
            )

            # Notify browser of state change
            try:
                request.env["bus.bus"]._sendone(
                    "gohan_job_updates",
                    "gohan/job_state",
                    {"id": record.id, "state": "generating"},
                )
            except Exception:
                pass

            # Use postcommit to ensure data is committed before
            # background thread reads it (fixes race condition LG-3).
            # Capture artifacts off the request — the closure executes
            # post-commit and the request scope is gone by then.
            artifacts_for_bg = artifacts if artifacts_pending else None

            def _deferred():
                from ..models.gohan_job import _submit_bg
                # 1) Artifacts upload to S3 — async, must not block webhook.
                #    Mirrors leviathan._upload_artifacts_bg's failure
                #    semantics: an S3 failure here logs loudly but does NOT
                #    mark the job failed; PRD generation continues.
                if artifacts_for_bg:
                    _submit_bg(
                        f"artifacts-upload[job={record_id}]",
                        record._upload_artifacts_bg,
                        db_name, record_id, artifacts_for_bg, s3_config,
                    )
                # 2) PRD generation — runs concurrently with the upload
                _submit_bg(
                    f"prd-gen[job={record_id}]",
                    record._run_prd_generation_bg, db_name, record_id,
                )

            request.env.cr.postcommit.add(_deferred)

            return Response(
                json.dumps({"status": "success", "next_step": "generating"}),
                status=200,
                content_type="application/json",
            )

        except psycopg2.errors.SerializationFailure:
            # Concurrent webhook for the same job committed a state change
            # after our REPEATABLE READ snapshot was taken. Our UPDATE
            # conflicted; the peer worker already persisted the callback.
            # Return 200 so Lambda's _send_callback stops retrying.
            # Pipeline event uses its own cursor so this still records.
            request.env.cr.rollback()
            record._append_pipeline_event(
                db_name, record_id, "extraction.callback", "ignored",
                message="Serialization conflict: peer worker already "
                        "persisted this callback (40001). Duplicate "
                        "dropped (not a failure).",
            )
            return Response(
                json.dumps({
                    "ignored": True,
                    "reason": "Serialization conflict (peer committed)",
                }),
                status=200,
                content_type="application/json",
            )
        except Exception as exc:
            _logger.exception(
                "Webhook processing failed for job %s", job_id
            )
            record._append_pipeline_event(
                db_name, record_id, "extraction.callback", "error",
                message=f"Webhook handler crashed while persisting callback: {exc}",
            )
            record._mark_failed(str(exc))
            return Response(
                json.dumps({"error": "Internal server error"}),
                status=500,
                content_type="application/json",
            )

    @http.route(
        "/gohan/webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def webhook_spec(self, **kwargs):
        """Spec-aligned run-complete webhook (HANDOFF_ODOO.md §4.5).

        Auth: ``X-Gohan-Signature`` header = hex HMAC-SHA256 of the raw
        request body using ``gohan.hmac_secret``. Constant-time verified.

        Body (JSON)::

            {
              "run_id": 42,
              "status": "done" | "failed",
              "score": 96.5,
              "qc_verdict": "shippable" | "not_shippable" | "pending",
              "eq_tier": "AUTHENTICATED" | "API_DOCS"
                       | "MARKETING_RICH" | "MARKETING_ONLY",
              "s3_artifact_prefix": "s3://gohan-artifacts/runs/42/",
              "lambda_request_id": "abc-123-def",
              "error_message": "..."   // failed only
            }

        ``run_id`` is treated as the ``gohan.job.id``. On ``done`` the job
        is finalized with the spec payload; on ``failed`` the error
        message is recorded and state flips to ``failed``.
        """
        raw_body = request.httprequest.data or b""
        if not _verify_hmac_signature(raw_body):
            _logger.warning(
                "[gohan][webhook] HMAC verification failed -- rejecting"
            )
            return Response(
                json.dumps({"error": "invalid signature"}),
                status=401,
                content_type="application/json",
            )

        try:
            body = json.loads(raw_body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return Response(
                json.dumps({"error": "invalid JSON body"}),
                status=400,
                content_type="application/json",
            )

        run_id = body.get("run_id")
        status = body.get("status")
        if not run_id or status not in ("done", "failed"):
            return Response(
                json.dumps({
                    "error": (
                        "missing run_id or status (must be 'done' or 'failed')"
                    )
                }),
                status=400,
                content_type="application/json",
            )

        try:
            run_id_int = int(run_id)
        except (TypeError, ValueError):
            return Response(
                json.dumps({"error": "run_id must be an integer"}),
                status=400,
                content_type="application/json",
            )

        job = request.env["gohan.job"].sudo().browse(run_id_int)
        if not job.exists():
            return Response(
                json.dumps({"error": f"run {run_id_int} not found"}),
                status=404,
                content_type="application/json",
            )

        # CRITICAL FIX (stuck-extracting parity): lock the row so concurrent
        # spec-webhook retries from Lambda are SERIALIZED. Same rationale
        # as the legacy webhook above -- without this lock, multiple Lambda
        # retries race on state writes and any silent transaction rollback
        # at end-of-request leaves the job in the wrong state.
        request.env.cr.execute(
            "SELECT state FROM gohan_job WHERE id=%s FOR UPDATE",
            (job.id,),
        )
        job.invalidate_recordset(["state"])

        # Idempotency: if the job is already in a terminal state from a
        # prior delivery of this callback, ack and return without
        # re-writing. Mirrors the legacy webhook's idempotency guard.
        if job.state in ("done", "failed", "submitted", "cancelled", "discarded"):
            _logger.info(
                "[gohan][webhook][job=%s] /gohan/webhook ignored: "
                "already in terminal state '%s'",
                job.name, job.state,
            )
            return Response(
                json.dumps({"ignored": True, "state": job.state}),
                status=200,
                content_type="application/json",
            )

        now = fields.Datetime.now()
        if status == "done":
            job.write({
                "state": "done",
                "score": float(body.get("score") or 0.0),
                "qc_verdict": body.get("qc_verdict") or "pending",
                "eq_tier": body.get("eq_tier") or False,
                "s3_artifact_prefix": body.get("s3_artifact_prefix") or False,
                "lambda_request_id": body.get("lambda_request_id") or False,
                "completed_at": now,
                "last_heartbeat": now,
                "lambda_callback_json": body,
            })
            # CRITICAL FIX (stuck-extracting parity): force the SQL UPDATE
            # to run NOW and durably commit it before we return 200. If
            # we don't, a later end-of-request error rolls the state
            # write back and the job appears stuck even though we ack'd.
            job.flush_recordset()
            request.env.cr.commit()
            try:
                job._notify_state_change("done")
            except Exception:
                _logger.exception(
                    "[gohan][webhook][job=%s] _notify_state_change failed",
                    job.name,
                )
            _logger.info(
                "[gohan][webhook][job=%s] finalized via /gohan/webhook "
                "(score=%s, qc=%s, eq=%s)",
                job.name,
                body.get("score"),
                body.get("qc_verdict"),
                body.get("eq_tier"),
            )
        else:
            error_msg = body.get("error_message") or "Pipeline reported failure"
            job.write({
                "state": "failed",
                "error_message": str(error_msg)[:500],
                "lambda_request_id": body.get("lambda_request_id") or False,
                "completed_at": now,
                "last_heartbeat": now,
                "lambda_callback_json": body,
            })
            # Same durability guarantee as the done branch above.
            job.flush_recordset()
            request.env.cr.commit()
            try:
                job._notify_state_change("failed")
            except Exception:
                _logger.exception(
                    "[gohan][webhook][job=%s] _notify_state_change failed",
                    job.name,
                )
            _logger.warning(
                "[gohan][webhook][job=%s] marked failed via /gohan/webhook: %s",
                job.name, error_msg,
            )

        return Response(
            json.dumps({"status": "ok"}),
            status=200,
            content_type="application/json",
        )

    @http.route(
        "/api/v1/gohan/jobs/<int:job_id>/status",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def get_job_status(self, job_id, **kwargs):
        """Job status API."""
        try:
            record = request.env["gohan.job"].browse(job_id)
            if not record.exists():
                return Response(
                    json.dumps({"error": "Job not found"}),
                    status=404,
                    content_type="application/json",
                )

            result = {
                "id": record.id,
                "name": record.name,
                "url": record.url,
                "state": record.state,
                "category": record.category_id.name if record.category_id else None,
                "score": record.score,
                "grade": record.grade,
                "qc_verdict": record.qc_verdict,
                "prd_url": record.prd_url,
                "duration_seconds": record.duration_seconds,
                "llm_attempts": record.llm_attempts,
                "error_message": record.error_message,
                "started_at": record.started_at.isoformat() if record.started_at else None,
                "completed_at": record.completed_at.isoformat() if record.completed_at else None,
            }
            return Response(
                json.dumps({"result": result}),
                status=200,
                content_type="application/json",
            )
        except Exception as exc:
            _logger.exception("Job status API failed for %s", job_id)
            return Response(
                json.dumps({"error": "Internal server error"}),
                status=500,
                content_type="application/json",
            )
