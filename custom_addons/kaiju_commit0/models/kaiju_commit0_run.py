# -*- coding: utf-8 -*-
import logging


from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

RUN_WORKFLOW_TEMPLATE = "kaiju-run-pipeline"

# Maps Odoo model_name selection values to pipeline model_short directory names.
# Must match PRESET_SHORT in eks/pipeline/init_run.py and run_stage.py.
MODEL_SHORT = {
    "opus": "opus4.6",
    "sonnet": "sonnet4",
    "haiku": "haiku3.5",
    "kimi": "kimi-k2.5",
    "glm5": "glm-5",
    "minimax": "minimax-m2.5",
    "nova_premier": "nova-premier",
    "nova_lite": "nova-2-lite",
    "gpt54": "gpt-5.4",
}


class KaijuCommit0Run(models.Model):
    _name = "kaiju.commit0.run"
    _description = "Commit0 Evaluation Run"
    _order = "create_date desc"

    name = fields.Char(string="Run ID", readonly=True, copy=False, default="New")
    build_id = fields.Many2one(
        "kaiju.commit0", string="Build", required=True, ondelete="cascade"
    )

    # ── Run Configuration ────────────────────────────────────────────────────

    model_name = fields.Selection(
        [
            ("opus", "Opus 4.6"),
            ("sonnet", "Sonnet 4"),
            ("haiku", "Haiku 3.5"),
            ("kimi", "Kimi K2.5"),
            ("glm5", "GLM-5"),
            ("minimax", "Minimax M2.5"),
            ("nova_premier", "Nova Premier"),
            ("nova_lite", "Nova Lite"),
            ("gpt54", "GPT-5.4"),
        ],
        string="Model",
        required=True,
        default="opus",
    )
    num_samples = fields.Integer(
        string="Samples",
        default=1,
        help="Number of trajectory samples per test",
    )
    max_iteration = fields.Integer(
        string="Max Iterations",
        default=3,
        help="Maximum draft-lint-test iterations",
    )
    use_spec_info = fields.Boolean(
        string="Use Spec Info",
        default=True,
        help="Include specification in LLM context",
    )

    # ── Advanced Run Configuration ─────────────────────────────────────

    skip_to_stage = fields.Integer(
        string="Skip to Stage",
        default=1,
        help="Resume from stage N (1=start fresh, 2=skip stage1, 3=skip stage1+2)",
    )
    inactivity_timeout = fields.Integer(
        string="Inactivity Timeout (s)",
        default=900,
        help="Kill agent if no log activity for this many seconds",
    )
    stage_timeout = fields.Integer(
        string="Stage Timeout (s)",
        default=0,
        help="Max time per stage (0=unlimited)",
    )
    max_wall_time = fields.Integer(
        string="Max Wall Time (s)",
        default=86400,
        help="Absolute maximum wall-clock time for entire pipeline",
    )
    eval_timeout = fields.Integer(
        string="Eval Timeout (s)",
        default=3600,
        help="Timeout for each evaluation step",
    )
    max_test_output_length = fields.Integer(
        string="Max Test Output Length",
        default=15000,
        help="Truncate test output beyond this many characters",
    )
    no_stage3_lint = fields.Boolean(
        string="No Stage 3 Lint",
        default=False,
        help="Disable lint info in stage 3 (ablation flag)",
    )

    # ── Run Status ───────────────────────────────────────────────────────────

    run_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Status",
        default="pending",
    )
    run_start = fields.Datetime(string="Run Start", readonly=True)
    run_end = fields.Datetime(string="Run End", readonly=True)
    run_log = fields.Text(string="Run Log")
    workflow_name = fields.Char(string="Run Workflow", readonly=True)
    s3_log_prefix = fields.Char(
        string="S3 Log Prefix",
        readonly=True,
        help="S3 key prefix where workflow step logs are stored "
        "(e.g. kaiju_logs/RepoName/123/). Set by the Argo exit-hook callback.",
    )
    step_ids = fields.One2many(
        "kaiju.commit0.workflow.step", "run_id", string="Workflow Steps"
    )

    # ── Log summary (computed from step_ids) ────────────────────────

    error_summary = fields.Char(
        string="Error Summary",
        compute="_compute_error_summary",
        store=False,
        help="Truncated error from first failed step — quick triage in list view",
    )
    combined_log_text = fields.Text(
        string="Combined Logs",
        compute="_compute_combined_log_text",
        store=False,
    )
    step_count = fields.Integer(
        string="Step Count",
        compute="_compute_step_count",
        store=False,
    )

    # ── Metrics ──────────────────────────────────────────────────────────────

    pass_rate = fields.Float(string="Pass Rate (%)", digits=(5, 1), readonly=True)
    tests_passed = fields.Integer(string="Tests Passed", readonly=True)
    tests_failed = fields.Integer(string="Tests Failed", readonly=True)
    tests_total = fields.Integer(string="Total Tests", readonly=True)
    duration_seconds = fields.Float(string="Duration (s)", digits=(6, 1), readonly=True)
    cost_usd = fields.Float(string="Cost (USD)", digits=(8, 4), readonly=True)
    tokens_input = fields.Integer(string="Input Tokens", readonly=True)
    tokens_output = fields.Integer(string="Output Tokens", readonly=True)

    # ── Related (for display) ────────────────────────────────────────────────

    repo_name = fields.Char(related="build_id.repo_name", store=False)
    language = fields.Selection(related="build_id.language", store=False)
    image_uri = fields.Char(related="build_id.image_uri", store=False)

    # ── Computed ─────────────────────────────────────────────────────────────

    is_running = fields.Boolean(compute="_compute_is_running", store=False)

    @api.depends("run_status")
    def _compute_is_running(self):
        for rec in self:
            rec.is_running = rec.run_status == "running"

    @api.depends(
        "step_ids.phase",
        "step_ids.message",
        "step_ids.step_name",
        "step_ids.log_text",
    )
    def _compute_error_summary(self):
        for rec in self:
            failed = rec.step_ids.filtered(
                lambda s: s.phase in ("Failed", "Error")
            ).sorted("started_at")
            if not failed:
                rec.error_summary = False
                continue
            first = failed[0]
            step_label = first.step_name or first.node_id or "unknown"
            msg = (first.message or "").strip()
            if not msg and first.log_text:
                tail = [l for l in first.log_text.strip().splitlines() if l.strip()]
                msg = tail[-1].strip() if tail else ""
            if not msg:
                msg = "Sync logs for details"
            if len(msg) > 200:
                msg = msg[:197] + "…"
            rec.error_summary = f"{step_label}: {msg}"

    @api.depends(
        "step_ids.log_text",
        "step_ids.step_name",
        "step_ids.phase",
        "step_ids.started_at",
        "step_ids.finished_at",
    )
    def _compute_combined_log_text(self):
        for rec in self:
            steps = rec.step_ids.sorted("started_at")
            if not steps:
                rec.combined_log_text = False
                continue
            parts = []
            for i, step in enumerate(steps, start=1):
                header_bits = [
                    f"[{i}/{len(steps)}]",
                    step.step_name or step.node_id or "step",
                    f"({step.phase or 'Pending'})",
                ]
                if step.started_at:
                    header_bits.append(
                        f"started {step.started_at.strftime('%H:%M:%S')}"
                    )
                if step.finished_at:
                    header_bits.append(
                        f"finished {step.finished_at.strftime('%H:%M:%S')}"
                    )
                header = " ".join(header_bits)
                separator = (
                    "─" * 6 + " " + header + " " + "─" * max(6, 100 - len(header) - 14)
                )
                body = step.log_text or "(no log captured)"
                parts.append(f"{separator}\n{body}")
            rec.combined_log_text = "\n\n".join(parts)

    @api.depends("step_ids")
    def _compute_step_count(self):
        for rec in self:
            rec.step_count = len(rec.step_ids)

    # ── CRUD ─────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("kaiju.commit0.run") or "New"
                )
        return super().create(vals_list)

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_run(self):
        """Submit run workflow to Argo Server (stages + eval + finalize)."""
        self.ensure_one()
        build = self.build_id
        if not build.image_uri:
            raise UserError("Build must be complete with a valid image before running.")
        if self.run_status == "done":
            raise UserError("Run already complete.")
        if self.run_status == "running":
            raise UserError("Run is already in progress.")

        argo = self.env["kaiju.argo.client"]
        repo_short = (build.repo_name or "unknown").split("/")[-1]

        callback_url = self._get_run_callback_url()

        # Map Odoo selection keys to pipeline preset names
        model_preset_map = {
            "nova_premier": "nova-premier",
            "nova_lite": "nova-lite",
        }

        model_preset = model_preset_map.get(self.model_name, self.model_name)

        parameters = {
            "repo_name": build.repo_name,
            "language": build.language,
            "branch_name": "commit0_combined",
            "model_preset": model_preset,
            "num_samples": str(self.num_samples),
            "max_iteration": str(self.max_iteration),
            "use_spec_info": "true" if self.use_spec_info else "false",
            "odoo_job_id": str(self.id),
            "callback_url": callback_url,
            "skip_to_stage": str(self.skip_to_stage or 1),
            "inactivity_timeout": str(self.inactivity_timeout or 900),
            "stage_timeout": str(self.stage_timeout or 0),
            "max_wall_time": str(self.max_wall_time or 86400),
            "eval_timeout": str(self.eval_timeout or 3600),
            "max_test_output_length": str(self.max_test_output_length or 15000),
            "no_stage3_lint": "true" if self.no_stage3_lint else "false",
        }

        # Submit workflow(s) — for pass@k, submit N workflows with sample_index=0..N-1
        num_samples = max(1, self.num_samples or 1)
        workflow_names = []

        for sample_idx in range(num_samples):
            sample_params = dict(parameters)
            sample_params["sample_index"] = str(sample_idx)

            try:
                wf_name = argo.submit_workflow(
                    RUN_WORKFLOW_TEMPLATE,
                    sample_params,
                    labels={
                        "kaiju/run-id": self.name,
                        "kaiju/repo": repo_short,
                        "kaiju/sample-index": str(sample_idx),
                    },
                )
                workflow_names.append(wf_name)
            except RuntimeError as e:
                _logger.error(
                    "Failed to submit run workflow sample %d for %s: %s",
                    sample_idx, self.name, e,
                )
                if not workflow_names:
                    raise UserError(f"Failed to submit run workflow: {e}") from e
                _logger.warning(
                    "Partial submission: %d/%d samples submitted for %s",
                    len(workflow_names), num_samples, self.name,
                )
                break

        wf_display = ", ".join(workflow_names)
        log_msg = (
            f"Submitted {len(workflow_names)} workflow(s): {wf_display}\n"
            f"Waiting for pipeline..."
        )

        self.write(
            {
                "run_status": "running",
                "run_start": fields.Datetime.now(),
                "workflow_name": workflow_names[0] if workflow_names else "",
                "run_log": log_msg,
            }
        )

    def _get_run_callback_url(self):
        ICP = self.env["ir.config_parameter"].sudo()
        base_url = ICP.get_param(
            "kaiju.odoo_internal_url", "http://odoo-web.odoo.svc:8069"
        )
        return f"{base_url}/kaiju/callback/run"

    def action_abort(self):
        """Stop running workflow via Argo Server."""
        self.ensure_one()
        if self.run_status != "running":
            raise UserError("Run is not currently in progress.")

        if self.workflow_name:
            argo = self.env["kaiju.argo.client"]
            try:
                argo.stop_workflow(
                    self.workflow_name, f"Aborted from Odoo ({self.name})"
                )
            except RuntimeError as e:
                _logger.warning("Failed to stop workflow %s: %s", self.workflow_name, e)

        self.write(
            {
                "run_status": "failed",
                "run_end": fields.Datetime.now(),
            }
        )

    # ── S3 Log Sync (exit-hook based) ───────────────────────────────

    def action_sync_logs(self):
        """Download step log files from S3 for one or more runs.

        Works on single record (form button) and multi-record (list
        Action menu).  Silently skips records without *s3_log_prefix*.
        """
        from odoo.exceptions import UserError

        eligible = self.filtered(lambda r: r.s3_log_prefix)
        if not eligible:
            raise UserError(
                "None of the selected runs have an S3 log prefix yet. "
                "Logs are uploaded by the Argo exit hook on workflow completion."
            )
        errors = []
        for rec in eligible:
            try:
                with self.env.cr.savepoint():
                    rec._fetch_logs_from_s3()
            except Exception as e:
                errors.append(f"{rec.name}: {e}")
                _logger.warning("Sync logs failed for %s: %s", rec.name, e)
            # Also try to fetch pipeline metrics (best-effort, independent of log sync)
            try:
                rec._fetch_pipeline_metrics()
            except Exception:
                pass

        if errors and len(errors) == len(eligible):
            raise UserError(
                f"All {len(errors)} sync(s) failed:\n\n" + "\n".join(errors)
            )
        if errors:
            _logger.warning(
                "Partial log sync: %d/%d failed:\n%s",
                len(errors),
                len(eligible),
                "\n".join(errors),
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Partial Sync",
                    "message": (
                        f"Synced {len(eligible) - len(errors)} of "
                        f"{len(eligible)} runs.  {len(errors)} failed "
                        f"— check server logs for details."
                    ),
                    "type": "warning",
                    "sticky": True,
                    "next": {"type": "ir.actions.client", "tag": "reload"},
                },
            }
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_sync_all_logs(self):
        """Sync logs for every terminal run that has an S3 log prefix."""
        from odoo.exceptions import UserError

        eligible = self.search(
            [
                ("s3_log_prefix", "!=", False),
                ("run_status", "in", ["done", "failed"]),
            ]
        )
        if not eligible:
            raise UserError("No completed runs need log syncing.")
        errors = []
        for rec in eligible:
            try:
                with self.env.cr.savepoint():
                    rec._fetch_logs_from_s3()
            except Exception as e:
                errors.append(f"{rec.name}: {e}")
                _logger.warning("Sync all logs failed for %s: %s", rec.name, e)

        if errors and len(errors) == len(eligible):
            raise UserError(
                f"All {len(errors)} sync(s) failed:\n\n" + "\n".join(errors)
            )
        if errors:
            _logger.warning(
                "Bulk log sync: %d/%d failed:\n%s",
                len(errors),
                len(eligible),
                "\n".join(errors),
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Partial Sync",
                    "message": (
                        f"Synced {len(eligible) - len(errors)} of "
                        f"{len(eligible)} runs.  {len(errors)} failed "
                        f"— check server logs for details."
                    ),
                    "type": "warning",
                    "sticky": True,
                    "next": {"type": "ir.actions.client", "tag": "reload"},
                },
            }
        return {"type": "ir.actions.client", "tag": "reload"}

    def _parse_s3_log_location(self):
        from odoo.exceptions import UserError

        raw = (self.s3_log_prefix or "").strip()
        if not raw:
            raise UserError("No S3 log prefix set on this record.")

        ICP = self.env["ir.config_parameter"].sudo()

        if raw.startswith("s3://"):
            rest = raw[5:]
            if "/" not in rest:
                raise UserError(f"Malformed S3 log prefix: {raw}")
            bucket, prefix = rest.split("/", 1)
        else:
            bucket = ICP.get_param("kaiju.s3_log_bucket", "production-grtlabs-tag")
            prefix = raw

        if prefix and not prefix.endswith("/"):
            prefix += "/"

        region = ICP.get_param("kaiju.aws_region", "ap-south-1")
        access_key = ICP.get_param("kaiju.aws_access_key_id", "") or None
        secret_key = ICP.get_param("kaiju.aws_secret_access_key", "") or None

        return bucket, prefix, region, access_key, secret_key

    def _fetch_logs_from_s3(self):
        from odoo.exceptions import UserError

        self.ensure_one()
        bucket, prefix, region, access_key, secret_key = self._parse_s3_log_location()

        _logger.info(
            "Sync logs for %s: bucket=%s prefix=%s",
            self.name,
            bucket,
            prefix,
        )

        try:
            import boto3
        except ImportError:
            raise UserError("boto3 is not installed in the Odoo Python environment.")

        client_kwargs = {"region_name": region}
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key

        try:
            s3 = boto3.client("s3", **client_kwargs)
        except Exception as e:
            raise UserError(f"Failed to create S3 client: {e}")

        self._reconcile_steps_from_manifest(s3, bucket, prefix)

        steps_with_logfile = [s for s in self.step_ids if s.log_file]

        if not steps_with_logfile:
            raise UserError(
                f"No steps with log files found.\n\n"
                f"S3 prefix: s3://{bucket}/{prefix}\n"
                f"Total steps: {len(self.step_ids)}\n"
                f"Steps with log_file: 0\n\n"
                f"Check that the Argo exit hook sent steps[] with log_file "
                f"in the callback payload."
            )

        fetched = 0
        failed = 0
        errors = []
        for step in steps_with_logfile:
            key = prefix + step.log_file
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
                log_content = obj["Body"].read().decode("utf-8", errors="replace")
                step.write(
                    {
                        "log_text": log_content,
                        "log_fetched_at": fields.Datetime.now(),
                    }
                )
                fetched += 1
            except Exception as e:
                _logger.warning(
                    "S3 log fetch failed for step %s (key=s3://%s/%s): %s",
                    step.step_name or step.node_id,
                    bucket,
                    key,
                    e,
                )
                errors.append(f"  {step.step_name or step.node_id}: {key} → {e}")
                failed += 1

        _logger.info(
            "S3 log sync for %s: fetched=%d failed=%d total_steps=%d",
            self.name,
            fetched,
            failed,
            len(self.step_ids),
        )

        if fetched == 0 and failed > 0:
            raise UserError(
                f"All {failed} log downloads failed:\n\n" + "\n".join(errors)
            )

    def _reconcile_steps_from_manifest(self, s3, bucket, prefix):
        """Ensure step records exist and have correct log_file, phase, and
        step_name via manifest.json.

        Handles three scenarios with a single S3 call:
        - No step records → create from manifest (callback was lost)
        - Steps missing log_file → backfill from manifest
        - Steps with stale phase/name (Argo API was unavailable in exit hook)
          → patch from manifest data
        Skips silently if manifest.json is absent or all steps already OK.
        """
        _VALID_PHASES = {"Succeeded", "Failed", "Error", "Skipped", "Omitted"}

        needs_work = not self.step_ids or any(
            not s.log_file or s.phase == "Pending" for s in self.step_ids
        )
        if not needs_work:
            return

        import json

        manifest_key = prefix + "manifest.json"
        try:
            obj = s3.get_object(Bucket=bucket, Key=manifest_key)
            manifest = json.loads(obj["Body"].read())
        except Exception as e:
            _logger.info(
                "No manifest.json for %s at s3://%s/%s: %s",
                self.name,
                bucket,
                manifest_key,
                e,
            )
            return

        steps_data = manifest.get("steps", [])
        if not steps_data:
            return

        if not self.step_ids:
            Step = self.env["kaiju.commit0.workflow.step"]
            for entry in steps_data:
                order = entry.get("order", 0)
                Step.create(
                    {
                        "run_id": self.id,
                        "node_id": f"manifest-{order}",
                        "step_name": entry.get("name", f"step-{order}"),
                        "phase": entry.get("phase", "Succeeded"),
                        "log_file": entry.get("log_file", ""),
                        "step_order": order,
                        "node_type": "Pod",
                    }
                )
            return

        manifest_by_name = {}
        manifest_by_order = {}
        for entry in steps_data:
            name = entry.get("name", "")
            order = entry.get("order", 0)
            if name:
                manifest_by_name[name] = entry
            if order:
                manifest_by_order[order] = entry

        updated = 0
        for step in self.step_ids:
            entry = manifest_by_name.get(step.step_name) or manifest_by_order.get(
                step.step_order
            )
            if not entry:
                continue
            vals = {}
            if not step.log_file and entry.get("log_file"):
                vals["log_file"] = entry["log_file"]
            if step.phase == "Pending" and entry.get("phase") in _VALID_PHASES:
                vals["phase"] = entry["phase"]
            m_name = entry.get("name", "")
            if m_name and step.step_name and len(step.step_name) > 40:
                vals["step_name"] = m_name
            if vals:
                step.write(vals)
                updated += 1

        if updated:
            _logger.info(
                "Reconciled %d steps for %s from manifest",
                updated,
                self.name,
            )

    # ── Pipeline Results Fetch (metrics from S3) ────────────────────

    def _fetch_pipeline_metrics(self):
        """Download pipeline_results.json from the dataset S3 bucket and
        populate run metrics (pass_rate, cost, test counts).

        Called automatically on callback receipt.  Best-effort: logs
        warnings but never raises."""
        self.ensure_one()
        build = self.build_id
        if not build.repo_name or not build.language or not self.model_name:
            _logger.info("Skipping metrics fetch for %s: missing repo/lang/model", self.name)
            return

        import json
        try:
            import boto3
        except ImportError:
            _logger.warning("boto3 not installed — cannot fetch pipeline metrics")
            return

        ICP = self.env["ir.config_parameter"].sudo()
        bucket = ICP.get_param("kaiju.s3_log_bucket", "production-grtlabs-tag")
        region = ICP.get_param("kaiju.aws_region", "ap-south-1")
        access_key = ICP.get_param("kaiju.aws_access_key_id", "") or None
        secret_key = ICP.get_param("kaiju.aws_secret_access_key", "") or None

        client_kwargs = {"region_name": region}
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key

        try:
            s3 = boto3.client("s3", **client_kwargs)
        except Exception as e:
            _logger.warning("S3 client creation failed for metrics fetch: %s", e)
            return

        # Build dataset S3 prefix: datasets/{lang}/{org}_{repo}/logs_{model_short}/
        repo_parts = build.repo_name.split("/")
        org = repo_parts[0].lower() if len(repo_parts) > 1 else "unknown"
        repo_short = repo_parts[-1].lower()
        model_short = MODEL_SHORT.get(self.model_name, self.model_name)
        prefix = f"datasets/{build.language.lower()}/{org}_{repo_short}/logs_{model_short}/"

        # Find latest run_N/
        try:
            paginator = s3.get_paginator("list_objects_v2")
            run_numbers = []
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
                for cp in page.get("CommonPrefixes", []):
                    dirname = cp["Prefix"][len(prefix):].rstrip("/")
                    if dirname.startswith("run_"):
                        try:
                            run_numbers.append(int(dirname[4:]))
                        except ValueError:
                            pass
        except Exception as e:
            _logger.warning("S3 listing failed for %s: %s", prefix, e)
            return

        if not run_numbers:
            _logger.info("No run_N dirs found under s3://%s/%s", bucket, prefix)
            return

        latest_run = max(run_numbers)
        results_key = f"{prefix}run_{latest_run}/pipeline_results.json"

        try:
            obj = s3.get_object(Bucket=bucket, Key=results_key)
            data = json.loads(obj["Body"].read())
        except Exception as e:
            _logger.info(
                "pipeline_results.json not found at s3://%s/%s: %s",
                bucket, results_key, e,
            )
            return

        # Extract metrics from the JSON (matches upstream run_pipeline.sh schema)
        vals = {}
        # Stage 3 pass_rate is the final metric; fall back to stage 2, then stage 1
        for stage_key in ["stage3", "stage2", "stage1"]:
            stage = data.get(stage_key, {})
            if stage.get("pass_rate") is not None:
                vals["pass_rate"] = round(stage["pass_rate"] * 100, 1)
                vals["tests_passed"] = stage.get("num_passed", 0)
                vals["tests_total"] = stage.get("num_tests", 0)
                vals["tests_failed"] = vals["tests_total"] - vals["tests_passed"]
                break

        # Cost: use stage3 cumulative if available, else sum all stages
        s3_data = data.get("stage3", {})
        if "cost_usd_cumulative" in s3_data:
            vals["cost_usd"] = s3_data["cost_usd_cumulative"]
        else:
            total_cost = 0.0
            for sk in ["stage1", "stage2", "stage3"]:
                sd = data.get(sk, {})
                total_cost += sd.get("cost_usd", 0.0) or sd.get("cost_usd_incremental", 0.0)
            if total_cost:
                vals["cost_usd"] = total_cost

        # Duration: sum all stage elapsed times
        total_elapsed = sum(
            data.get(sk, {}).get("elapsed_s", 0) for sk in ["stage1", "stage2", "stage3"]
        )
        if total_elapsed:
            vals["duration_seconds"] = total_elapsed

        if vals:
            self.write(vals)
            _logger.info(
                "Populated metrics for %s from s3://%s/%s: %s",
                self.name, bucket, results_key, vals,
            )

    @staticmethod
    def _append_log(existing_log, new_line):
        from datetime import datetime

        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {new_line}"
        if existing_log:
            return f"{existing_log}\n{entry}"
        return entry
