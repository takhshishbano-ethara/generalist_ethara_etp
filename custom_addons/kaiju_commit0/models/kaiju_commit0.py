# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timezone, timedelta

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PHASE_ORDER = ["config", "build"]

BUILD_WORKFLOW_TEMPLATE = "kaiju-build-pipeline"

# ── Pipeline metadata constants (mirror commit_0/eks/pipeline/prepare.py) ──
HF_REPO_ID = "ethara/Kaiju-phase2"
HF_BASE_URL = "https://huggingface.co/datasets"
FORK_ORG = "Zahgon"
GITHUB_BASE_URL = "https://github.com"


def _parse_argo_dt(value):
    """Parse an Argo RFC3339 timestamp into a naive UTC datetime suitable
    for an Odoo Datetime field. Returns False on empty / invalid input."""
    if not value:
        return False
    try:
        # Argo emits e.g. '2026-05-14T06:16:53Z' or '...+00:00'
        text = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return False

class KaijuCommit0(models.Model):
    _name = "kaiju.commit0"
    _description = "Commit0 Build"
    _order = "create_date desc"

    # ── Configuration ────────────────────────────────────────────────────────

    name = fields.Char(string="Build ID", readonly=True, copy=False, default="New")
    repo_name = fields.Char(string="Repository", required=True)
    language = fields.Selection(
        [
            ("python", "Python"),
            ("java", "Java"),
            ("go", "Go"),
            ("typescript", "TypeScript"),
            ("rust", "Rust"),
        ],
        string="Language",
        required=True,
        default="python",
    )
    language_version = fields.Char(
        string="Language Version",
        help="e.g. 3.11, 11, 1.21, 18, 1.75.0",
    )
    # NOTE: branch_name field removed (was vestigial; pipeline hardcodes
    # "commit0_all" in commit_0/tools/prepare_repo_*.py). Constant value
    # passed below preserves identical repo_hash / image tags.

    # ── Navigation ───────────────────────────────────────────────────────────

    # current_phase auto-flips to 'build' once a workflow is submitted or the
    # build leaves 'pending'. The user does not have to click Next on existing
    # records — they land directly on the Build screen.
    # Inverse method preserves user navigation (Next/Back) within a session;
    # on form reload, compute reasserts 'build' for any non-pending record.
    current_phase = fields.Selection(
        [
            ("config", "Configuration"),
            ("build", "Build"),
        ],
        string="Current Phase",
        compute="_compute_current_phase",
        inverse="_inverse_current_phase",
        store=True,
        readonly=False,
        default="config",
    )

    @api.depends("workflow_name", "build_status")
    def _compute_current_phase(self):
        """Auto-land on Build phase for any record that has progressed past initial config.

        Triggers:
          - workflow_name set (Argo submission happened) → 'build'
          - build_status not in ('pending', False) → 'build'
          - Otherwise → 'config' (new records)

        User can still click Back via the navigation bar; the inverse stores the
        override. On next form load / recompute, this method will reassert 'build'
        for any non-pending record, which is the desired behavior for existing builds.
        """
        for rec in self:
            if rec.workflow_name or (rec.build_status and rec.build_status != "pending"):
                rec.current_phase = "build"
            else:
                rec.current_phase = "config"

    def _inverse_current_phase(self):
        """No-op inverse — stored value is written automatically.

        Required because Selection with compute+store+readonly=False needs an
        inverse to allow direct writes from Next/Back navigation actions and
        the stepper widget.
        """
        # The framework persists the assigned value; nothing else to do.
        return

    # ── Phase: Config Validation ─────────────────────────────────────────────

    # Defaults to True — manual "Validate Configuration" button removed;
    # validation happens inline in action_run_build and _compute_navigation_flags.
    # Kept as a field so existing data, Reset button visibility, and the wizard's
    # back-compat action_validate_config() call continue to work.
    config_valid = fields.Boolean(string="Config Validated", default=True)

    # ── Phase: Build (prepare + build-repo-image) ────────────────────────────

    build_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Build Status",
        default="pending",
    )
    build_start = fields.Datetime(string="Build Start", readonly=True)
    build_end = fields.Datetime(string="Build End", readonly=True)
    build_log = fields.Text(string="Build Log")

    image_uri = fields.Char(string="Image URI", readonly=True)
    s3_dataset_uri = fields.Char(string="Dataset S3 URI", readonly=True)
    dataset_file = fields.Binary(
        string="Dataset (dataset_entries.json)",
        readonly=True,
        attachment=True,
        help="Cached copy of dataset_entries.json downloaded from S3 on metadata fetch. "
             "Click to view/download in Odoo without needing S3 access.",
    )
    dataset_filename = fields.Char(
        string="Dataset Filename",
        readonly=True,
        default="dataset_entries.json",
    )
    workflow_name = fields.Char(string="Build Workflow", readonly=True)

    # ── Log summary (computed from step_ids) ────────────────────────

    error_summary = fields.Char(
        string="Error Summary",
        compute="_compute_error_summary",
        store=True,
        help="Truncated error from first failed step — quick triage in list view",
    )
    combined_log_text = fields.Text(
        string="Combined Logs",
        compute="_compute_combined_log_text",
        store=False,
        help="All step logs concatenated in chronological order with step headers",
    )
    step_count = fields.Integer(
        string="Step Count",
        compute="_compute_step_count",
        store=False,
    )

    # ── Pipeline metadata (extracted from S3 dataset_entries.json on completion) ─

    instance_id = fields.Char(string="Instance ID", readonly=True,
        help="Stable identifier from dataset_entries.json (e.g. 'go-version_go')")
    forked_repo = fields.Char(string="Forked Repo", readonly=True,
        help="Forked repository name (org/name) from dataset_entries.json")
    base_commit = fields.Char(string="Base Commit", readonly=True,
        help="Stubbed code commit SHA — starting point for the agent")
    reference_commit = fields.Char(string="Reference Commit", readonly=True,
        help="Original code commit SHA — ground truth for evaluation")
    metadata_fetched_at = fields.Datetime(string="Metadata Fetched At", readonly=True)
    metadata_error = fields.Char(string="Metadata Fetch Error", readonly=True)

    # ── Derived URLs (no DB column, all computed from repo_name/language) ────

    original_repo_url = fields.Char(string="Source Repo",
        compute="_compute_pipeline_urls", store=False)
    forked_repo_url = fields.Char(string="Forked Repo URL",
        compute="_compute_pipeline_urls", store=False)
    hf_dataset_url = fields.Char(string="HuggingFace Dataset",
        compute="_compute_pipeline_urls", store=False)
    test_ids_url = fields.Char(string="Test IDs (GitHub)",
        compute="_compute_pipeline_urls", store=False)
    setup_sh_url = fields.Char(string="setup.sh (S3)",
        compute="_compute_artifact_urls", store=False)
    dockerfile_url = fields.Char(string="Dockerfile (S3)",
        compute="_compute_artifact_urls", store=False)

    # ── Runs ─────────────────────────────────────────────────────────────────

    run_ids = fields.One2many("kaiju.commit0.run", "build_id", string="Runs")
    step_ids = fields.One2many(
        "kaiju.commit0.workflow.step", "build_id", string="Workflow Steps"
    )
    run_count = fields.Integer(string="Run Count", compute="_compute_run_count")

    # ── Computed ─────────────────────────────────────────────────────────────

    is_build_running = fields.Boolean(compute="_compute_is_build_running", store=False)
    can_go_next = fields.Boolean(compute="_compute_navigation_flags", store=False)
    can_go_back = fields.Boolean(compute="_compute_navigation_flags", store=False)
    can_create_run = fields.Boolean(compute="_compute_can_create_run", store=False)

    @api.depends("run_ids")
    def _compute_run_count(self):
        for rec in self:
            rec.run_count = len(rec.run_ids)

    @api.depends("build_status")
    def _compute_is_build_running(self):
        for rec in self:
            rec.is_build_running = rec.build_status == "running"

    @api.depends("step_ids.phase", "step_ids.message", "step_ids.display_name")
    def _compute_error_summary(self):
        """Find first failed step and surface its error message for quick triage."""
        for rec in self:
            failed = rec.step_ids.filtered(
                lambda s: s.phase in ("Failed", "Error")
            ).sorted("started_at")
            if failed:
                first = failed[0]
                step_label = first.display_name or first.node_id or "unknown"
                msg = (first.message or "(no error message)").strip()
                if len(msg) > 200:
                    msg = msg[:197] + "…"
                rec.error_summary = f"{step_label}: {msg}"
            else:
                rec.error_summary = False

    @api.depends("step_ids.log_text", "step_ids.display_name",
                 "step_ids.phase", "step_ids.started_at", "step_ids.finished_at")
    def _compute_combined_log_text(self):
        """Concatenate all step logs chronologically with separators."""
        for rec in self:
            steps = rec.step_ids.sorted("started_at")
            if not steps:
                rec.combined_log_text = False
                continue
            parts = []
            for i, step in enumerate(steps, start=1):
                header_bits = [
                    f"[{i}/{len(steps)}]",
                    step.display_name or step.node_id or "step",
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
                separator = "─" * 6 + " " + header + " " + "─" * max(
                    6, 100 - len(header) - 14
                )
                body = step.log_text or "(no log captured)"
                parts.append(f"{separator}\n{body}")
            rec.combined_log_text = "\n\n".join(parts)

    @api.depends("step_ids")
    def _compute_step_count(self):
        for rec in self:
            rec.step_count = len(rec.step_ids)

    @api.depends("repo_name", "language")
    def _compute_pipeline_urls(self):
        """Compute static URLs derivable from repo_name + language."""
        for rec in self:
            repo_name = rec.repo_name or ""
            language = rec.language or ""
            short = repo_name.split("/")[-1] if "/" in repo_name else repo_name

            if repo_name and "/" in repo_name:
                rec.original_repo_url = f"{GITHUB_BASE_URL}/{repo_name}"
            else:
                rec.original_repo_url = False

            if short:
                rec.forked_repo_url = f"{GITHUB_BASE_URL}/{FORK_ORG}/{short}"
                rec.test_ids_url = (
                    f"{GITHUB_BASE_URL}/{FORK_ORG}/{short}/blob/commit0_all/{short}.bz2"
                )
            else:
                rec.forked_repo_url = False
                rec.test_ids_url = False

            # Hugging Face URL format:
            #   https://huggingface.co/datasets/ethara/Kaiju-phase2/tree/main/datasets/{language}/{repo_with_underscore}
            # where {repo_with_underscore} = repo_name with '/' replaced by '_'
            # e.g. language=go, repo_name=trustmaster/goflow -> trustmaster_goflow
            if repo_name and language:
                repo_underscored = repo_name.replace("/", "_")
                rec.hf_dataset_url = (
                    f"{HF_BASE_URL}/{HF_REPO_ID}/tree/main/datasets/"
                    f"{language}/{repo_underscored}"
                )
            else:
                rec.hf_dataset_url = False

    @api.depends("s3_dataset_uri")
    def _compute_artifact_urls(self):
        """Derive setup.sh and Dockerfile S3 URLs from the dataset URI
        (build-repo-image uploads them next to dataset_entries.json)."""
        for rec in self:
            uri = rec.s3_dataset_uri or ""
            if uri.startswith("s3://") and "/" in uri[5:]:
                # s3://bucket/key/path/dataset_entries.json -> base = s3://bucket/key/path
                base = uri.rsplit("/", 1)[0]
                rec.setup_sh_url = f"{base}/setup.sh"
                rec.dockerfile_url = f"{base}/Dockerfile"
            else:
                rec.setup_sh_url = False
                rec.dockerfile_url = False

    @api.depends("current_phase", "repo_name", "language", "build_status")
    def _compute_navigation_flags(self):
        for rec in self:
            idx = (
                PHASE_ORDER.index(rec.current_phase)
                if rec.current_phase in PHASE_ORDER
                else 0
            )
            rec.can_go_back = idx > 0

            if rec.current_phase == "config":
                # Inline validation: repo must be set in owner/repo format.
                rec.can_go_next = bool(
                    rec.repo_name and "/" in rec.repo_name and rec.language
                )
            else:
                rec.can_go_next = False

    @api.depends("build_status")
    def _compute_can_create_run(self):
        for rec in self:
            rec.can_create_run = rec.build_status == "done"

    # ── CRUD ─────────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("kaiju.commit0") or "New"
                )
        return super().create(vals_list)

    # ── Navigation Actions ───────────────────────────────────────────────────

    def action_next_phase(self):
        self.ensure_one()
        idx = (
            PHASE_ORDER.index(self.current_phase)
            if self.current_phase in PHASE_ORDER
            else 0
        )
        if idx < len(PHASE_ORDER) - 1:
            self.current_phase = PHASE_ORDER[idx + 1]

    def action_prev_phase(self):
        self.ensure_one()
        idx = (
            PHASE_ORDER.index(self.current_phase)
            if self.current_phase in PHASE_ORDER
            else 0
        )
        if idx > 0:
            self.current_phase = PHASE_ORDER[idx - 1]

    # ── Phase Actions ────────────────────────────────────────────────────────

    def action_validate_config(self):
        self.ensure_one()
        if not self.repo_name:
            raise UserError("Repository name is required.")
        if "/" not in self.repo_name:
            raise UserError("Repository must be in 'owner/repo' format.")
        self.write({"config_valid": True})

    def action_run_build(self):
        """Submit build workflow to Argo Server (prepare + build-repo-image)."""
        self.ensure_one()
        # Inline validation — the manual "Validate Configuration" button was removed.
        if not self.repo_name:
            raise UserError("Repository name is required.")
        if "/" not in self.repo_name:
            raise UserError("Repository must be in 'owner/repo' format (e.g. 'Zahgon/gson').")
        if not self.language:
            raise UserError("Language is required.")
        if self.build_status == "done":
            raise UserError("Build already complete. Create a new run instead.")
        if self.build_status == "running":
            raise UserError("Build is already running.")

        argo = self.env["kaiju.argo.client"]
        repo_short = (self.repo_name or "unknown").split("/")[-1]

        # repo_hash: short identifier for image tagging
        import hashlib

        repo_hash = hashlib.sha256(
            f"{self.repo_name}:commit0_combined".encode()
        ).hexdigest()[:12]

        callback_url = self._get_build_callback_url()

        parameters = {
            "repo_name": self.repo_name,
            "repo_hash": repo_hash,
            "language": self.language,
            "branch_name": "commit0_combined",
            "odoo_job_id": str(self.id),
            "callback_url": callback_url,
        }

        try:
            workflow_name = argo.submit_workflow(
                BUILD_WORKFLOW_TEMPLATE,
                parameters,
                labels={"kaiju/build-id": self.name, "kaiju/repo": repo_short},
            )
        except RuntimeError as e:
            _logger.error("Failed to submit build workflow for %s: %s", self.name, e)
            raise UserError(f"Failed to submit build workflow: {e}") from e

        self.write(
            {
                "build_status": "running",
                "build_start": fields.Datetime.now(),
                "workflow_name": workflow_name,
                "build_log": f"Workflow submitted: {workflow_name}\nWaiting for pipeline...",
            }
        )

    def _get_build_callback_url(self):
        """Construct the callback URL for the build pipeline to call on completion."""
        ICP = self.env["ir.config_parameter"].sudo()
        base_url = ICP.get_param(
            "kaiju.odoo_internal_url", "http://odoo-web.odoo.svc:8069"
        )
        return f"{base_url}/kaiju/callback/build"

    def action_create_run(self):
        self.ensure_one()
        if self.build_status != "done":
            raise UserError("Build must be complete before creating a run.")
        return {
            "type": "ir.actions.act_window",
            "name": "New Run",
            "res_model": "kaiju.commit0.run",
            "view_mode": "form",
            "context": {"default_build_id": self.id},
            "target": "current",
        }

    def action_view_runs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Runs for {self.name}",
            "res_model": "kaiju.commit0.run",
            "view_mode": "list,form",
            "domain": [("build_id", "=", self.id)],
            "context": {"default_build_id": self.id},
        }

    def action_abort(self):
        """Stop running build workflow via Argo Server."""
        self.ensure_one()
        if self.build_status != "running":
            raise UserError("No build is currently running.")

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
                "build_status": "failed",
                "build_end": fields.Datetime.now(),
            }
        )

    def action_reset(self):
        self.ensure_one()
        if self.build_status == "running":
            raise UserError("Cannot reset while build is running. Abort first.")
        self.write(
            {
                "current_phase": "config",
                "config_valid": False,
                "build_status": "pending",
                "build_start": False,
                "build_end": False,
                "build_log": False,
                "image_uri": False,
                "s3_dataset_uri": False,
                "workflow_name": False,
                "instance_id": False,
                "forked_repo": False,
                "base_commit": False,
                "reference_commit": False,
                "metadata_fetched_at": False,
                "metadata_error": False,
            }
        )

    # ── Cron: Poll Argo for running builds ───────────────────────────────────

    @api.model
    def _cron_poll_build_status(self):
        """Called by ir.cron every 60s to update running builds from Argo."""
        running_builds = self.search(
            [("build_status", "=", "running"), ("workflow_name", "!=", False)]
        )
        # Defense in depth: also re-poll terminal builds that have no step records
        # yet OR have steps without logs. Catches the race where callback finalizes
        # status before _sync_steps runs (and before pod GC at 5min destroys pods).
        # Bounded to recently-finished builds (last 10min) to avoid scanning history.
        recent_cutoff = fields.Datetime.now() - timedelta(minutes=10)
        terminal_incomplete = self.search([
            ("build_status", "in", ("done", "failed")),
            ("workflow_name", "!=", False),
            ("build_end", ">=", recent_cutoff),
        ]).filtered(
            lambda b: not b.step_ids or any(not s.log_text for s in b.step_ids)
        )
        all_builds = running_builds | terminal_incomplete
        if not all_builds:
            return

        argo = self.env["kaiju.argo.client"]
        for build in all_builds:
            try:
                status = argo.get_workflow_status(build.workflow_name)
            except RuntimeError as e:
                _logger.warning(
                    "Failed to poll build %s (workflow %s): %s",
                    build.name,
                    build.workflow_name,
                    e,
                )
                continue

            phase = status.get("phase", "")
            progress = status.get("progress", "")
            previous_status = build.build_status

            # Sync per-step records on every poll so the UI updates progressively
            try:
                build._sync_steps()
            except Exception as e:
                _logger.warning(
                    "Failed to sync steps for build %s: %s", build.name, e
                )

            # Incrementally persist logs for any finished step that doesn't
            # yet have logs cached. This ensures logs land in Postgres well
            # before Argo's pod GC (default 5min) deletes the source pods.
            try:
                build._persist_step_logs_incremental()
            except Exception as e:
                _logger.warning(
                    "Incremental log persist failed for build %s: %s", build.name, e
                )

            # Skip status write if already in terminal state (callback won the race).
            # We still want to run _sync_steps/_persist_step_logs (above) for back-fill,
            # but we MUST NOT overwrite the callback-set build_end / image_uri.
            if build.build_status in ("done", "failed"):
                pass  # callback already finalized; back-fill steps/logs only
            elif phase in ("Succeeded",):
                build.write(
                    {
                        "build_status": "done",
                        "build_end": fields.Datetime.now(),
                        "build_log": self._append_log(
                            build.build_log, f"Workflow completed. Progress: {progress}"
                        ),
                    }
                )
            elif phase in ("Failed", "Error"):
                message = status.get("message", "Unknown error")
                build.write(
                    {
                        "build_status": "failed",
                        "build_end": fields.Datetime.now(),
                        "build_log": self._append_log(
                            build.build_log, f"Workflow failed: {phase} — {message}"
                        ),
                    }
                )
            elif phase in ("Running", "Pending", ""):
                build.write(
                    {
                        "build_log": self._append_log(
                            build.build_log,
                            f"Status: {phase or 'Pending'} ({progress})",
                        ),
                    }
                )

            # On running → terminal transition, persist all step logs once
            # so they survive Argo pod garbage collection.
            if (
                previous_status == "running"
                and build.build_status in ("done", "failed")
            ):
                try:
                    build._persist_step_logs()
                except Exception as e:
                    _logger.warning(
                        "Failed to persist step logs for build %s: %s",
                        build.name,
                        e,
                    )
                # Also pull rich metadata from S3 once on completion
                try:
                    build._fetch_pipeline_metadata()
                except Exception as e:
                    _logger.warning(
                        "Failed to fetch pipeline metadata for build %s: %s",
                        build.name,
                        e,
                    )

    # ── Step Sync & Log Persistence ─────────────────────────────────

    def _sync_steps(self):
        """Fetch workflow nodes from Argo and upsert step records."""
        self.ensure_one()
        if not self.workflow_name:
            return
        argo = self.env["kaiju.argo.client"]
        nodes = argo.list_workflow_nodes(self.workflow_name)
        if not nodes:
            return
        Step = self.env["kaiju.commit0.workflow.step"]
        existing = {s.node_id: s for s in self.step_ids}
        for node in nodes:
            node_id = node.get("id") or ""
            if not node_id:
                continue
            vals = {
                "display_name": node.get("displayName") or node.get("name") or node_id,
                "pod_name": node.get("podName") or node_id,
                "template_name": node.get("templateName") or "",
                "node_type": node.get("type") or "Pod",
                "phase": node.get("phase") or "Pending",
                "message": node.get("message") or "",
                "started_at": _parse_argo_dt(node.get("startedAt")),
                "finished_at": _parse_argo_dt(node.get("finishedAt")),
            }
            if node_id in existing:
                existing[node_id].write(vals)
            else:
                vals["node_id"] = node_id
                vals["build_id"] = self.id
                Step.create(vals)

    def _persist_step_logs(self):
        """Fetch and persist logs for every Pod-type step. Called once on
        running → terminal transition so logs survive Argo pod GC."""
        self.ensure_one()
        for step in self.step_ids:
            if step.node_type != "Pod" or not step.pod_name:
                continue
            step.action_fetch_logs()

    def _persist_step_logs_incremental(self):
        """Fetch logs ONLY for steps that have finished but don't yet have logs.

        Called on every cron poll to capture step logs as soon as each pod
        completes — well before Argo's pod garbage collection (typically 5min)
        kicks in. Idempotent: skips steps that already have log_text.
        """
        self.ensure_one()
        terminal_phases = ("Succeeded", "Failed", "Error")
        for step in self.step_ids:
            if step.node_type != "Pod" or not step.pod_name:
                continue
            if step.phase not in terminal_phases:
                continue
            if step.has_log:
                continue  # already captured — don't re-fetch
            try:
                step.action_fetch_logs()
            except Exception as e:
                _logger.warning(
                    "Incremental log fetch failed for step %s (pod=%s): %s",
                    step.display_name or step.node_id,
                    step.pod_name,
                    e,
                )

    def action_fetch_all_step_logs(self):
        """Manual trigger: sync step list from Argo, then fetch logs for each step.

        Use this when auto-persist on completion didn't fire (e.g. workflow
        already finished before the module was upgraded), or to refresh
        logs while the workflow is still running."""
        self.ensure_one()
        if not self.workflow_name:
            from odoo.exceptions import UserError
            raise UserError("No workflow has been submitted for this build yet.")
        try:
            self._sync_steps()
        except Exception as e:
            _logger.warning("action_fetch_all_step_logs: _sync_steps failed: %s", e)
        self.step_ids.action_fetch_logs()
        return {"type": "ir.actions.client", "tag": "reload"}

    # ── Pipeline Metadata (S3 dataset_entries.json) ─────────────────────

    def _fetch_pipeline_metadata(self):
        """Download dataset_entries.json from S3 and extract identity fields.

        Populates: instance_id, forked_repo, base_commit, reference_commit.
        URL fields are computed separately from repo_name/language.

        Requires System Parameters:
            kaiju.aws_access_key_id     (or rely on EC2/IRSA instance profile)
            kaiju.aws_secret_access_key (or rely on EC2/IRSA instance profile)
            kaiju.aws_region            (default: ap-south-1)
        """
        self.ensure_one()
        if not self.s3_dataset_uri:
            self.metadata_error = "No s3_dataset_uri set; nothing to fetch"
            return
        if not self.s3_dataset_uri.startswith("s3://"):
            self.metadata_error = f"Invalid S3 URI: {self.s3_dataset_uri}"
            return

        try:
            import boto3  # noqa: F401  (lazy import: requires `pip install boto3`)
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError:
            self.metadata_error = "boto3 not installed in Odoo venv"
            _logger.error(
                "boto3 missing in Odoo venv — cannot fetch S3 metadata for %s", self.name
            )
            return

        # Parse s3://bucket/key
        rest = self.s3_dataset_uri[5:]
        if "/" not in rest:
            self.metadata_error = f"Malformed S3 URI: {self.s3_dataset_uri}"
            return
        bucket, key = rest.split("/", 1)

        ICP = self.env["ir.config_parameter"].sudo()
        region = ICP.get_param("kaiju.aws_region", "ap-south-1")
        access_key = ICP.get_param("kaiju.aws_access_key_id", "") or None
        secret_key = ICP.get_param("kaiju.aws_secret_access_key", "") or None

        try:
            import boto3
            client_kwargs = {"region_name": region}
            if access_key and secret_key:
                client_kwargs["aws_access_key_id"] = access_key
                client_kwargs["aws_secret_access_key"] = secret_key
            s3 = boto3.client("s3", **client_kwargs)
            obj = s3.get_object(Bucket=bucket, Key=key)
            payload = obj["Body"].read()
        except (BotoCoreError, ClientError, Exception) as e:
            self.metadata_error = f"S3 fetch failed: {e}"
            _logger.warning("S3 fetch failed for build %s: %s", self.name, e)
            return

        import json
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            self.metadata_error = f"Invalid JSON in S3 object: {e}"
            return

        # dataset_entries.json is a list of entries; find the one matching this repo
        entries = data if isinstance(data, list) else data.get("data", [data])
        repo_short = (self.repo_name or "").split("/")[-1]
        matched = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_repo = entry.get("original_repo") or entry.get("repo") or ""
            if self.repo_name and (self.repo_name in entry_repo or entry_repo in self.repo_name):
                matched = entry
                break
            if repo_short and repo_short in entry_repo:
                matched = entry
                break
        if not matched and entries and isinstance(entries[0], dict):
            matched = entries[0]  # single-entry case

        if not matched:
            self.metadata_error = (
                f"No entry matching {self.repo_name} found in dataset_entries.json"
            )
            return

        # Cache the raw dataset file as a binary attachment so users can
        # view/download it inside Odoo without needing S3 access.
        import base64
        dataset_filename = key.rsplit("/", 1)[-1] if key else "dataset_entries.json"
        try:
            dataset_b64 = base64.b64encode(payload)
        except Exception as e:
            _logger.warning(
                "Failed to base64-encode dataset payload for build %s: %s", self.name, e,
            )
            dataset_b64 = False

        self.write({
            "instance_id": matched.get("instance_id") or "",
            "forked_repo": matched.get("repo") or "",
            "base_commit": matched.get("base_commit") or "",
            "reference_commit": matched.get("reference_commit") or "",
            "metadata_fetched_at": fields.Datetime.now(),
            "metadata_error": False,
            "dataset_file": dataset_b64,
            "dataset_filename": dataset_filename,
        })
        _logger.info(
            "Fetched pipeline metadata for build %s: instance=%s base=%s ref=%s",
            self.name,
            matched.get("instance_id"),
            (matched.get("base_commit") or "")[:12],
            (matched.get("reference_commit") or "")[:12],
        )

    def action_fetch_pipeline_metadata(self):
        """Manual trigger to pull metadata from S3 (e.g. if auto-fetch failed
        or the s3_dataset_uri was set after completion)."""
        self.ensure_one()
        if not self.s3_dataset_uri:
            raise UserError(
                "No S3 dataset URI on this build yet. "
                "Wait until the prepare step finishes."
            )
        self._fetch_pipeline_metadata()
        if self.metadata_error:
            raise UserError(f"Metadata fetch failed: {self.metadata_error}")
        return {"type": "ir.actions.client", "tag": "reload"}

    @staticmethod
    def _append_log(existing_log, new_line):
        from datetime import datetime

        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {new_line}"
        if existing_log:
            return f"{existing_log}\n{entry}"
        return entry
