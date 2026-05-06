"""Stage 6: Trajectory Generation — K8s Job Dispatch.

Dispatches a single K8s Job per repository that runs the full
run_custom_eval.sh pipeline (inference + evaluation + summary).
"""
import json
import logging
import os
import uuid

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class JaegerRepositoryStage6(models.Model):
    _inherit = "jaeger.repository"

    # ── Stage 6 Actions ──────────────────────────────────────────────────

    def action_dispatch_trajectories(self):
        """Button handler: dispatch trajectory generation K8s Job."""
        self.ensure_one()
        if self.current_stage != "stage6":
            raise UserError("Repository must be in Stage 6 to generate trajectories.")
        if self.trajectory_status in ("dispatched", "running", "evaluating"):
            raise UserError("Trajectory generation is already in progress.")
        if not self.final_dataset_jsonl_path and not self.final_dataset_count:
            raise UserError("No finalized dataset found. Complete Stage 5 first.")

        # Validate LLM config
        ICP = self.env["ir.config_parameter"].sudo()
        llm_template = ICP.get_param("jaeger.llm_config_template", "")
        if not llm_template:
            raise UserError(
                "No LLM config template configured. "
                "Go to Settings → Jaeger → Trajectory Settings and set the LLM config template."
            )

        # Validate ECR prefix
        ecr_prefix = self.ecr_prefix or ICP.get_param("jaeger.ecr_prefix", "")
        if not ecr_prefix:
            raise UserError(
                "No ECR prefix configured. Set it on the repository or in Settings → Jaeger."
            )

        # Lock and dispatch
        self.env.cr.execute(
            "SELECT id FROM jaeger_repository WHERE id = %s FOR UPDATE NOWAIT",
            [self.id],
        )

        job_id = f"jaeger-traj-{self.id}-{uuid.uuid4().hex[:8]}"
        config = self._resolve_trajectory_config()

        self.write({
            "trajectory_status": "dispatched",
            "eks_job_id": job_id,
            "llm_config_json": json.dumps(config),
            "error_message": False,
        })

        # Dispatch K8s Job via post-commit hook
        repo_id = self.id
        db_name = self.env.cr.dbname

        def _dispatch_k8s():
            from odoo import SUPERUSER_ID, api
            from odoo.modules.registry import Registry
            registry = Registry(db_name)
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                repo = env["jaeger.repository"].browse(repo_id)
                if repo.exists():
                    repo._create_trajectory_k8s_job(config, job_id)

        self.env.cr.postcommit.add(_dispatch_k8s)
        self._append_log(f"Trajectory job dispatched: {job_id}")
        return True

    def _create_trajectory_k8s_job(self, config, job_id):
        """Create the K8s Job for trajectory generation."""
        from kubernetes import client
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        ICP = self.env["ir.config_parameter"].sudo()
        namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")
        traj_image = ICP.get_param(
            "jaeger.trajectory_image",
            "426628337772.dkr.ecr.ap-south-1.amazonaws.com/jaeger-trajectory:latest",
        )
        ecr_prefix = self.ecr_prefix or ICP.get_param("jaeger.ecr_prefix", "")
        sandbox = ICP.get_param("jaeger.sandbox_mode", "0") == "1"

        # Upload dataset to S3 for the pod to download
        dataset_s3_key = self._upload_dataset_for_trajectory()

        # Build webhook URL
        base_url = (
            os.environ.get("JAEGER_WEBHOOK_BASE_URL")
            or ICP.get_param("jaeger.webhook_base_url", "")
            or ICP.get_param("web.base.url", "http://localhost:8069")
        )
        webhook_url = f"{base_url.rstrip('/')}/jaeger/webhook/pipeline"

        # Upsert K8s Secret
        secret_name = f"jaeger-traj-{self.id}-secrets"
        secret_data = {
            "LLM_CONFIG_JSON": json.dumps(config),
        }
        if sandbox:
            secret_data["AWS_ACCESS_KEY_ID"] = ICP.get_param("jaeger.s3_access_key", "")
            secret_data["AWS_SECRET_ACCESS_KEY"] = ICP.get_param("jaeger.s3_secret_key", "")

        core_v1 = client.CoreV1Api()
        self._ensure_k8s_namespace(core_v1, namespace)
        self._upsert_k8s_secret(
            core_v1, namespace, secret_name,
            secret_data,
            {"app.kubernetes.io/name": "jaeger-trajectory", "repo-id": str(self.id)},
        )

        # Build env vars
        env_vars = [
            client.V1EnvVar(name="REPO_ID", value=str(self.id)),
            client.V1EnvVar(name="JOB_ID", value=job_id),
            client.V1EnvVar(name="DATASET_S3_KEY", value=dataset_s3_key),
            client.V1EnvVar(name="ECR_PREFIX", value=ecr_prefix),
            client.V1EnvVar(name="LANGUAGE", value=self.language or "python"),
            client.V1EnvVar(name="K_RUNS", value=str(config.get("k_runs", 8))),
            client.V1EnvVar(name="NUM_WORKERS", value=str(config.get("num_workers", 1))),
            client.V1EnvVar(name="MAX_ITERATIONS", value=str(config.get("max_iterations", 300))),
            client.V1EnvVar(name="MAX_RETRIES", value=str(config.get("max_retries", 3))),
            client.V1EnvVar(
                name="CONVERSATION_TIMEOUT",
                value=str(config.get("conversation_timeout", 3600)),
            ),
            client.V1EnvVar(name="TEMPERATURE", value=str(config.get("temperature", 1.0))),
            client.V1EnvVar(name="WEBHOOK_URL", value=webhook_url),
            client.V1EnvVar(
                name="S3_BUCKET",
                value=os.environ.get(
                    "JAEGER_S3_BUCKET", ICP.get_param("jaeger.s3_bucket", ""),
                ),
            ),
            client.V1EnvVar(
                name="S3_REGION",
                value=os.environ.get(
                    "JAEGER_S3_REGION", ICP.get_param("jaeger.s3_region", "ap-south-1"),
                ),
            ),
            client.V1EnvVar(name="S3_PREFIX", value="jaeger/phase3"),
            # From Secret
            client.V1EnvVar(
                name="LLM_CONFIG_JSON",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=secret_name, key="LLM_CONFIG_JSON",
                    ),
                ),
            ),
        ]

        # Container spec — privileged for DinD (runs Docker builds + containers)
        container = client.V1Container(
            name="trajectory",
            image=traj_image,
            env=env_vars,
            security_context=client.V1SecurityContext(privileged=True),
            resources=client.V1ResourceRequirements(
                requests={"cpu": "4", "memory": "8Gi", "ephemeral-storage": "50Gi"},
                limits={"memory": "32Gi", "ephemeral-storage": "200Gi"},
            ),
        )

        # Job spec
        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=f"jaeger-traj-{self.id}",
                namespace=namespace,
                labels={
                    "app.kubernetes.io/name": "jaeger-trajectory",
                    "app.kubernetes.io/component": "pipeline",
                    "repo-id": str(self.id),
                    "platform": "jaeger",
                    "jaeger-job-id": job_id,
                },
            ),
            spec=client.V1JobSpec(
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={
                        "app.kubernetes.io/name": "jaeger-trajectory",
                        "repo-id": str(self.id),
                    }),
                    spec=client.V1PodSpec(
                        containers=[container],
                        restart_policy="Never",
                        service_account_name="jaeger-pipeline-runner" if not sandbox else None,
                        node_selector={
                            "kubernetes.io/arch": "amd64",
                            "ethara.ai/node-pool": "build",
                        } if not sandbox else None,
                    ),
                ),
                backoff_limit=1,
                active_deadline_seconds=86400,
                ttl_seconds_after_finished=7200,
            ),
        )

        # Submit
        import time as _time
        batch_v1 = client.BatchV1Api()
        job_name = f"jaeger-traj-{self.id}"
        try:
            batch_v1.create_namespaced_job(namespace=namespace, body=job)
        except client.ApiException as e:
            if e.status == 409:
                _logger.warning("Trajectory Job %s already exists — recreating", job_name)
                batch_v1.delete_namespaced_job(
                    name=job_name, namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Foreground"),
                )
                _time.sleep(2)
                batch_v1.create_namespaced_job(namespace=namespace, body=job)
            else:
                raise
        self._append_log(f"K8s Job created: {job_name} in namespace {namespace}")

    def _upload_dataset_for_trajectory(self):
        """Upload the final dataset JSONL to S3 for the trajectory pod."""
        s3_prefix = "jaeger/phase3"
        s3_key = f"{s3_prefix}/{self.id}/final_dataset.jsonl"

        local_path = self.final_dataset_jsonl_path
        if local_path and os.path.exists(local_path):
            s3_bucket = os.environ.get("JAEGER_S3_BUCKET", "")
            if s3_bucket:
                import boto3
                s3_client = boto3.client(
                    "s3",
                    region_name=os.environ.get("JAEGER_S3_REGION", "ap-south-1"),
                )
                s3_client.upload_file(local_path, s3_bucket, s3_key)
            return s3_key

        # If already on S3 from phase1/2, construct the key
        phase1_prefix = os.environ.get("JAEGER_S3_PREFIX", "jaeger/phase1")
        mode = self.pipeline_mode or "swe"
        return (
            f"{phase1_prefix}/{mode}/{self.id}/"
            f"{self.org}__{self.repo_name}_final_dataset.jsonl"
        )

    # ── Webhook Handler ──────────────────────────────────────────────────

    def _handle_trajectory_webhook(self, status, results):
        """Handle trajectory webhook — routes to type-specific handlers.

        Called from the pipeline webhook controller when payload type
        starts with 'trajectory_'.
        """
        if status == "trajectory_progress":
            self._handle_trajectory_progress(results)
        elif status == "done":
            self._handle_trajectory_done(results)
        elif status == "failed":
            self._handle_trajectory_failed(results)
        elif status == "completed":
            # Legacy per-pod webhook (backward compat)
            self._handle_legacy_pod_result(status, results)
        else:
            _logger.warning(
                "Unknown trajectory webhook status '%s' for repo %s",
                status, self.name,
            )

    def _handle_trajectory_progress(self, data):
        """Handle trajectory progress update."""
        step = data.get("step", "")
        self._append_log(f"Trajectory: {step}")
        if self.trajectory_status != "running":
            self.write({"trajectory_status": "running"})

    def _handle_trajectory_done(self, data):
        """Handle trajectory completion — process summary and per-run results."""
        summary = data.get("summary") or {}
        per_run_results = data.get("per_run_results") or []
        s3_output_key = data.get("s3_output_key", "")

        # Create/update trajectory run records from per_run_results
        Run = self.env["jaeger.trajectory.run"]
        for run_data in per_run_results:
            run_num = run_data.get("run_number", 0)
            vals = {
                "status": "resolved" if run_data.get("resolved") else "unresolved",
                "resolved": run_data.get("resolved", False),
                "agent_patch": run_data.get("agent_patch", ""),
                "api_calls": run_data.get("api_calls", 0),
                "api_cost": run_data.get("api_cost", 0.0),
                "api_time_seconds": run_data.get("api_time", 0.0),
                "prompt_tokens": run_data.get("prompt_tokens", 0),
                "completion_tokens": run_data.get("completion_tokens", 0),
            }
            if run_data.get("report"):
                vals["eval_report_json"] = json.dumps(run_data["report"])
                vals["eval_status"] = "resolved" if run_data.get("resolved") else "unresolved"

            # Find existing or create — one run record per instance per run_number
            existing = Run.search([
                ("repository_id", "=", self.id),
                ("run_number", "=", run_num),
            ], limit=1)

            if existing:
                existing.write(vals)
            else:
                # Create run records for each valid instance
                for inst in self.instance_ids.filtered(lambda i: i.is_valid):
                    Run.create({
                        "name": f"{inst.name}-run-{run_num}",
                        "instance_id": inst.id,
                        "repository_id": self.id,
                        "run_number": run_num,
                        "model": self.model_canonical_name or "claude",
                        **vals,
                    })

        # Write summary
        self._summarize_trajectories_from_data(summary)

    def _handle_trajectory_failed(self, data):
        """Handle trajectory failure."""
        error = data.get("error", "Unknown error")
        log_tail = data.get("log_tail", "")
        error_msg = f"{error}\n\n{log_tail}" if log_tail else error
        self.write({
            "trajectory_status": "failed",
            "error_message": error_msg[:2000],
        })
        self._append_log(f"Trajectory FAILED: {error}")

    def _summarize_trajectories_from_data(self, summary):
        """Write summary from the run_custom_eval.sh output."""
        pass_at_k = summary.get("pass_at_k", 0.0)
        timing = summary.get("timing_metrics", {}).get("total", {})

        self.write({
            "trajectory_status": "done",
            "pass_at_k": pass_at_k,
            "pass_at_k_summary_json": json.dumps(summary, indent=2),
            "total_api_cost": timing.get("accumulated_cost_usd", 0.0),
            "total_api_calls": timing.get("api_calls", 0),
            "total_prompt_tokens": timing.get("prompt_tokens", 0),
            "total_completion_tokens": timing.get("completion_tokens", 0),
        })

        self._append_log(
            f"Trajectory complete: pass@{self.k_runs} = {pass_at_k:.4f}, "
            f"cost=${timing.get('accumulated_cost_usd', 0):.2f}"
        )

        # Auto-advance if gate passes
        gate_ok, _ = self._check_current_gate()
        if gate_ok:
            next_stage = self._next_stage()
            if next_stage:
                self.write({"current_stage": next_stage})

    # ── Legacy Per-Pod Webhook Handler (backward compat) ─────────────────

    def _handle_legacy_pod_result(self, status, results):
        """Handle incoming EKS webhook with per-pod trajectory run results.

        Legacy path for the old N×K pod architecture.
        """
        pod_name = results.get("pod_name", "")
        Run = self.env["jaeger.trajectory.run"]
        run = Run.search([("eks_pod_name", "=", pod_name)], limit=1)

        if not run:
            _logger.warning("No trajectory run found for pod %s", pod_name)
            return

        if status == "completed":
            run.write({
                "status": "resolved" if results.get("resolved") else "unresolved",
                "resolved": results.get("resolved", False),
                "agent_patch": results.get("agent_patch", ""),
                "conversation_log": results.get("conversation", ""),
                "api_calls": results.get("api_calls", 0),
                "api_cost": results.get("api_cost", 0.0),
                "api_time_seconds": results.get("api_time", 0.0),
                "prompt_tokens": results.get("prompt_tokens", 0),
                "completion_tokens": results.get("completion_tokens", 0),
                "duration_seconds": results.get("duration", 0.0),
            })
            if results.get("eval_report"):
                run.write({
                    "eval_status": "passed" if results.get("resolved") else "failed",
                    "eval_report_json": json.dumps(results["eval_report"]),
                    "eval_passed_count": results["eval_report"].get("passed", 0),
                    "eval_failed_count": results["eval_report"].get("failed", 0),
                })
        else:
            run.write({
                "status": "error",
                "conversation_log": results.get("error_message", "Unknown error"),
            })

        # Check if all runs for this repo are complete
        pending = self.run_ids.filtered(
            lambda r: r.status in ("queued", "running", "evaluating"),
        )
        if not pending:
            self._summarize_trajectories()

    # ── Config Resolution ────────────────────────────────────────────────

    def _resolve_trajectory_config(self):
        """Build trajectory configuration, merging per-repo overrides with system defaults."""
        ICP = self.env["ir.config_parameter"].sudo()

        config = {
            "model_name": self.model_canonical_name or ICP.get_param(
                "jaeger.default_model", "claude",
            ),
            "k_runs": self.k_runs or int(ICP.get_param("jaeger.default_k", "8")),
            "num_workers": self.num_workers or int(ICP.get_param(
                "jaeger.max_run_workers", "1",
            )),
            "max_iterations": self.max_iterations or 300,
            "max_retries": self.max_retries or 3,
            "conversation_timeout": self.conversation_timeout or int(ICP.get_param(
                "jaeger.conversation_timeout", "3600",
            )),
            "temperature": self.temperature if self.temperature else float(ICP.get_param(
                "jaeger.temperature", "1.0",
            )),
        }

        template_str = ICP.get_param("jaeger.llm_config_template", "{}")
        try:
            template = json.loads(template_str) if template_str else {}
        except (json.JSONDecodeError, TypeError):
            template = {}
        template.update(config)
        return template

    # ── ORM-Based Summarization (legacy/fallback for cron poll) ───────────

    def _summarize_trajectories(self):
        """Summarize trajectory results and compute pass@k from ORM run records."""
        self._append_log("All trajectory runs complete. Computing pass@k...")

        runs = self.run_ids
        total_runs = len(runs)
        resolved_runs = len(runs.filtered(lambda r: r.resolved))

        # Compute pass@k per instance
        instance_results = {}
        for run in runs:
            inst_name = run.instance_id.name
            if inst_name not in instance_results:
                instance_results[inst_name] = {"total": 0, "resolved": 0}
            instance_results[inst_name]["total"] += 1
            if run.resolved:
                instance_results[inst_name]["resolved"] += 1

        # pass@k = 1 - C(n-c, k) / C(n, k) where n=total, c=correct, k=k_runs
        k = self.k_runs or 8
        pass_at_k_values = []
        for inst_name, counts in instance_results.items():
            n = counts["total"]
            c = counts["resolved"]
            if n == 0:
                pass_at_k_values.append(0.0)
            elif c >= k:
                pass_at_k_values.append(1.0)
            else:
                # pass@k = 1 - prod((n-c-i)/(n-i) for i in range(k))
                numerator = 1.0
                for i in range(k):
                    if n - i == 0:
                        break
                    numerator *= (n - c - i) / (n - i)
                pass_at_k_values.append(1.0 - numerator)

        avg_pass_at_k = sum(pass_at_k_values) / max(len(pass_at_k_values), 1)

        # Aggregate costs
        total_cost = sum(r.api_cost or 0 for r in runs)
        total_calls = sum(r.api_calls or 0 for r in runs)
        total_prompt = sum(r.prompt_tokens or 0 for r in runs)
        total_completion = sum(r.completion_tokens or 0 for r in runs)

        summary = {
            "total_instances": len(instance_results),
            "total_runs": total_runs,
            "resolved_runs": resolved_runs,
            "pass_at_k": round(avg_pass_at_k, 4),
            "k": k,
            "per_instance": {
                name: {
                    "pass_at_k": round(pass_at_k_values[idx], 4),
                    **counts,
                }
                for idx, (name, counts) in enumerate(instance_results.items())
            },
            "total_cost": round(total_cost, 2),
            "total_api_calls": total_calls,
        }

        self.write({
            "trajectory_status": "done",
            "pass_at_k": avg_pass_at_k,
            "pass_at_k_summary_json": json.dumps(summary, indent=2),
            "total_api_cost": total_cost,
            "total_api_calls": total_calls,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
        })

        self._append_log(
            f"Trajectory summary: pass@{k} = {avg_pass_at_k:.4f}, "
            f"{resolved_runs}/{total_runs} resolved, cost=${total_cost:.2f}",
        )

        gate_ok, _ = self._check_current_gate()
        if gate_ok:
            next_stage = self._next_stage()
            if next_stage:
                self.write({"current_stage": next_stage})
