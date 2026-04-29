import logging

from odoo import models
from odoo.exceptions import UserError

from odoo.addons.jaeger.worker.pipeline_helpers import (
    PipelineCancelled,
    _append_log_standalone,
    _check_cancelled,
    _write_with_retry,
)

_logger = logging.getLogger(__name__)


class JaegerRepositoryStage4(models.Model):
    _inherit = "jaeger.repository"

    # ── Stage 4 Actions ──────────────────────────────────────────────────

    def action_run_tests(self):
        raise UserError(
            "Queue-based dispatch is disabled (RabbitMQ consumer deleted). "
            "Use the 'Run Tests (Direct)' button instead."
        )

    def action_run_tests_direct(self):
        self.ensure_one()
        if self.current_stage != "stage4":
            raise UserError("Repository must be in Stage 4.")
        built = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "built",
        )
        if not built:
            raise UserError("No built images found.")
        if self.test_execution_status in ("running", "queued"):
            raise UserError("Test execution is already in progress.")
        return self._run_pipeline_async(
            "_run_all_tests", "test_execution_status", "Test Execution",
        )

    def _run_all_tests(self):
        """Run test execution for all built instances in parallel."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from .jaeger_instance import _run_instance_tests_standalone

        self.ensure_one()
        self.write({"test_execution_status": "running", "error_message": False})
        self.env.cr.commit()

        built = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "built",
        )
        instance_ids = built.ids
        total = len(instance_ids)
        if not total:
            self.write({"test_execution_status": "done", "error_message": "No built instances"})
            self.env.cr.commit()
            return

        db_name = self.env.cr.dbname
        repo_id = self.id

        ICP = self.env["ir.config_parameter"].sudo()
        max_workers = int(ICP.get_param("jaeger.max_run_workers", "2"))
        agent_timeout = int(ICP.get_param("jaeger.agent_timeout", "1800"))

        _append_log_standalone(db_name, repo_id,
            f"Starting parallel test execution: {total} instances, {max_workers} workers")

        completed = valid_count = invalid_count = error_count = 0

        cancelled = False
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_run_instance_tests_standalone, db_name, iid, agent_timeout): iid
                for iid in instance_ids
            }
            for future in as_completed(futures):
                iid = futures[future]
                try:
                    res = future.result()
                except Exception as e:
                    _logger.error("Instance %s raised: %s", iid, e)
                    res = {"instance_id": iid, "success": False, "is_valid": False,
                           "error": str(e), "summary": f"exception: {e}"}

                completed += 1
                if res.get("is_valid"):
                    valid_count += 1
                elif res.get("success"):
                    invalid_count += 1
                if res.get("error"):
                    error_count += 1

                summary = res.get("summary") or res.get("error") or "done"
                _append_log_standalone(db_name, repo_id,
                    f"  [{completed}/{total}] instance #{iid}: {summary}")

                _write_with_retry(db_name, repo_id, {
                    "test_execution_progress": (completed / total) * 100,
                    "instances_tested_count": completed,
                    "instances_valid_count": valid_count,
                    "instances_invalid_count": invalid_count,
                    "instances_error_count": error_count,
                })

                try:
                    _check_cancelled(db_name, repo_id)
                except PipelineCancelled:
                    _append_log_standalone(db_name, repo_id, "Cancellation requested — stopping remaining instances")
                    for f in futures:
                        f.cancel()
                    cancelled = True
                    break

        if cancelled:
            _append_log_standalone(db_name, repo_id,
                f"Test execution cancelled: {completed}/{total} done, {valid_count} valid, {invalid_count} invalid")
        else:
            _append_log_standalone(db_name, repo_id,
                f"Test execution complete: {valid_count} valid, {invalid_count} invalid, {error_count} errors")

        vals = {"test_execution_status": "done", "terminal_state": "none", "error_message": False,
                "cancel_requested": False}
        if valid_count == 0 and completed > 0:
            vals["terminal_state"] = "no_valid_instances"
            vals["error_message"] = (
                "All %d tested instances are invalid — no fix signal detected."
                % completed
            )
        try:
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    vals["current_stage"] = next_stage
            self.write(vals)
            self.env.cr.commit()
        except Exception:
            _logger.warning("Final write via ORM failed, using standalone retry")
            _write_with_retry(db_name, repo_id, vals)

