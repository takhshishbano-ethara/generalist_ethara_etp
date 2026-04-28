import logging

from odoo import models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class JaegerRepositoryStage5(models.Model):
    _inherit = "jaeger.repository"

    # ── Stage 5 Actions ──────────────────────────────────────────────────

    def action_finalize_dataset(self):
        self.ensure_one()
        if self.current_stage != "stage5":
            raise UserError("Repository must be in Stage 5.")
        self.write({"dataset_status": "generating", "error_message": False})
        from ..services.rabbitmq_service import publish_finalize_task

        publish_finalize_task(self.id)

    def action_finalize_dataset_direct(self):
        self.ensure_one()
        if self.current_stage != "stage5":
            raise UserError("Repository must be in Stage 5.")
        if self.dataset_status in ("generating", "queued"):
            raise UserError("Dataset finalization is already in progress.")
        return self._run_pipeline_async(
            "run_dataset_finalization", "dataset_status", "Dataset Finalization",
        )

    def run_dataset_finalization(self):
        """Build final dataset JSONL. Called by consumer.py via XML-RPC."""
        self.ensure_one()
        self.write({"dataset_status": "generating", "error_message": False})
        self.env.cr.commit()
        try:
            self._build_final_dataset()
            vals = {"dataset_status": "done", "terminal_state": "none", "error_message": False}
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    vals["current_stage"] = next_stage
            self.write(vals)
            self.env.cr.commit()
        except Exception as e:
            self.env.cr.rollback()
            self.write(
                {
                    "dataset_status": "failed",
                    "error_message": str(e)[:2000],
                },
            )
            self.env.cr.commit()
            raise

    def _build_final_dataset(self):
        """Build final dataset JSONL from validated instances.

        Aggregates test results across all instances, writes the final
        dataset JSONL file, and produces a FinalReport with statistics.
        """
        import json
        from pathlib import Path

        ICP = self.env["ir.config_parameter"].sudo()
        output_dir = Path(ICP.get_param("jaeger.output_dir", "/tmp/jaeger_data"))
        out_dir = output_dir / f"{self.org}__{self.repo_name}"
        out_dir.mkdir(parents=True, exist_ok=True)

        self._append_log("Step 1/3: Counting instance results...")

        all_instances = self.instance_ids.filtered(
            lambda i: i.docker_build_status == "built" and i.report_json,
        )
        valid = all_instances.filtered(lambda i: i.is_valid)
        invalid = all_instances.filtered(lambda i: not i.is_valid)

        total = len(all_instances)
        valid_count = len(valid)
        invalid_count = len(invalid)
        error_count = len(self.instance_ids.filtered(
            lambda i: i.validation_error and "error" in (i.validation_error or "").lower(),
        ))
        empty_patch = len(self.instance_ids.filtered(
            lambda i: not i.fix_patch,
        ))

        self._append_log(
            f"  {total} tested, {valid_count} valid, "
            f"{invalid_count} invalid, {error_count} errors",
        )

        if valid_count == 0:
            self.write({
                "terminal_state": "no_valid_instances",
                "error_message": "No valid instances after test execution.",
            })
            raise ValueError(
                f"No valid instances for {self.org}/{self.repo_name}",
            )

        self._append_log("Step 2/3: Writing final dataset JSONL...")

        final_path = out_dir / f"{self.org}__{self.repo_name}_final_dataset.jsonl"
        count = 0
        with open(final_path, "w", encoding="utf-8") as f:
            for inst in valid:
                entry = {
                    "instance_id": inst.name,
                    "org": inst.org,
                    "repo": inst.repo,
                    "pr_number": inst.pr_number,
                    "base_sha": inst.base_sha,
                    "language": inst.language,
                    "fix_patch": inst.fix_patch or "",
                    "test_patch": inst.test_patch or "",
                    "f2p_tests": json.loads(inst.f2p_tests_json or "{}"),
                    "p2p_tests": json.loads(inst.p2p_tests_json or "{}"),
                    "s2p_tests": json.loads(inst.s2p_tests_json or "{}"),
                    "n2p_tests": json.loads(inst.n2p_tests_json or "{}"),
                    "fixed_tests": json.loads(inst.fixed_tests_json or "{}"),
                    "run_result": json.loads(inst.run_result_json or "{}"),
                    "test_patch_result": json.loads(inst.test_patch_result_json or "{}"),
                    "fix_patch_result": json.loads(inst.fix_patch_result_json or "{}"),
                    "docker_image_name": inst.docker_image_name or "",
                    "is_valid": True,
                    "tag": inst.tag or "",
                    "version": inst.tag or inst.base_sha or "",
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                count += 1

        self._append_log(f"  Wrote {count} entries to {final_path}")

        self._append_log("Step 3/3: Generating final report...")

        report = {
            "repository": f"{self.org}/{self.repo_name}",
            "total_instances": total,
            "valid_instances": valid_count,
            "invalid_instances": invalid_count,
            "error_instances": error_count,
            "empty_patch_instances": empty_patch,
            "f2p_total": sum(inst.f2p_count for inst in valid),
            "p2p_total": sum(inst.p2p_count for inst in valid),
        }

        report_path = out_dir / f"{self.org}__{self.repo_name}_final_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        self.write({
            "final_dataset_jsonl_path": str(final_path),
            "final_dataset_count": count,
            "final_report_json": json.dumps(report),
            "total_instances": total,
            "resolved_instances": valid_count,
            "unresolved_instances": invalid_count,
            "empty_patch_instances": empty_patch,
            "error_instances": error_count,
        })

        self._append_log(f"Finalization complete: {count} valid instances in final dataset")


    # ── Stage 7 Actions ──────────────────────────────────────────────────

    def action_export_meta(self):
        raise UserError("Phase 2-7 not available yet. Only Phase 1 (PR Collection) is active.")
        self.ensure_one()
        if self.current_stage != "stage7":
            raise UserError("Repository must be in Stage 7.")
        self.write({"delivery_status": "converting", "error_message": False})
        from ..services.rabbitmq_service import publish_export_task

        publish_export_task(self.id)

    def action_export_meta_direct(self):
        raise UserError("Phase 2-7 not available yet. Only Phase 1 (PR Collection) is active.")
        self.ensure_one()
        if self.current_stage != "stage7":
            raise UserError("Repository must be in Stage 7.")
        if self.delivery_status in ("converting", "queued"):
            raise UserError("Meta export is already in progress.")
        return self._run_pipeline_async(
            "run_meta_export", "delivery_status", "Meta Export",
        )

    def run_meta_export(self):
        """Convert to Meta delivery schema. Called by consumer.py via XML-RPC."""
        self.ensure_one()
        self.write({"delivery_status": "converting", "error_message": False})
        self.env.cr.commit()
        try:
            self._convert_to_meta_schema()
            vals = {"delivery_status": "done", "terminal_state": "none", "error_message": False}
            gate_ok, _ = self._check_current_gate()
            if gate_ok:
                next_stage = self._next_stage()
                if next_stage:
                    vals["current_stage"] = next_stage
            self.write(vals)
            self.env.cr.commit()
        except Exception as e:
            self.env.cr.rollback()
            self.write(
                {
                    "delivery_status": "failed",
                    "error_message": str(e)[:2000],
                },
            )
            self.env.cr.commit()
            raise

    def _convert_to_meta_schema(self):
        """Convert all valid instances to Meta delivery schema and generate JSONL."""
        import json
        from pathlib import Path

        ICP = self.env["ir.config_parameter"].sudo()
        output_dir = Path(ICP.get_param("jaeger.output_dir", "/tmp/jaeger_data"))
        out_dir = output_dir / f"{self.org}__{self.repo_name}"
        out_dir.mkdir(parents=True, exist_ok=True)

        self._append_log("Step 1/4: Running pre-flight validation...")

        candidates = self.instance_ids.filtered(
            lambda i: i.is_valid and i.docker_build_status == "built",
        )
        if not candidates:
            self.write({
                "error_message": "No valid instances for Meta export.",
                "terminal_state": "no_valid_instances",
            })
            raise ValueError("No valid instances for Meta export")

        self._append_log(f"  {len(candidates)} instances passed pre-flight")

        self._append_log("Step 2/4: Converting instances to Meta schema...")
        from ..tools.dataset_converter import MetaSchemaConverter

        ecr_prefix = ICP.get_param("jaeger.ecr_prefix", "")
        converter = MetaSchemaConverter(
            ecr_prefix=ecr_prefix,
            task_category=self.task_category or "hard_swe",
            repo_category=f"{self.language}_{self.pipeline_mode}",
        )

        converted, errors = converter.convert_batch(candidates)

        for inst_name, error in errors:
            self._append_log(f"  FAILED {inst_name}: {error}")

        self._append_log(
            f"  {len(converted)} converted, {len(errors)} failed",
        )

        if not converted:
            raise ValueError("All Meta schema conversions failed")

        # Update individual instance records
        for inst in candidates:
            try:
                meta_json = converter.convert(inst)
                inst.write({
                    "meta_schema_json": json.dumps(meta_json, ensure_ascii=False),
                    "delivery_status": "converted",
                })
            except Exception:
                pass  # Already tracked in errors

        self._append_log("Step 3/4: Writing Meta delivery JSONL...")

        delivery_path = out_dir / f"{self.org}__{self.repo_name}_meta_delivery.jsonl"
        with open(delivery_path, "w", encoding="utf-8") as f:
            for entry in converted:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._append_log(f"  Wrote {len(converted)} entries to {delivery_path}")

        self._append_log("Step 4/4: Generating delivery summary...")

        summary = {
            "repository": f"{self.org}/{self.repo_name}",
            "total_candidates": len(candidates),
            "converted": len(converted),
            "failed": len(errors),
            "delivery_path": str(delivery_path),
        }
        summary_path = out_dir / f"{self.org}__{self.repo_name}_delivery_summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        self.write({
            "meta_delivery_jsonl_path": str(delivery_path),
            "delivered_count": len(converted),
        })

        delivered = self.instance_ids.filtered(
            lambda i: i.delivery_status == "converted",
        )
        delivered.write({"delivery_status": "delivered"})

        self._append_log(
            f"Export complete: {len(converted)} instances delivered to {delivery_path}",
        )
