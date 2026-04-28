import logging
import os

from odoo import models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class JaegerRepositoryStage6(models.Model):
    _inherit = "jaeger.repository"

    # ── Stage 6 Actions ──────────────────────────────────────────────────

    def action_dispatch_trajectories(self):
        raise UserError("Phase 2-7 not available yet. Only Phase 1 (PR Collection) is active.")
        self.ensure_one()
        if self.current_stage != "stage6":
            raise UserError("Repository must be in Stage 6.")
        self.write({"trajectory_status": "dispatched", "error_message": False})
        from ..services.rabbitmq_service import publish_trajectory_task

        publish_trajectory_task(self.id)

    def run_trajectory_dispatch(self):
        """Dispatch trajectory generation to EKS. Called by consumer.py via XML-RPC."""
        self.ensure_one()
        self.write({"trajectory_status": "running", "error_message": False})
        try:
            self._dispatch_to_eks()
        except Exception as e:
            self.write(
                {
                    "trajectory_status": "failed",
                    "error_message": str(e)[:2000],
                },
            )
            raise

    def _dispatch_to_eks(self):
        """Dispatch trajectory generation jobs to EKS.

        For each valid instance, creates K pods on EKS (one per pass@k run).
        Each pod runs the SWE agent with the configured LLM model, receives
        the problem statement, and produces a patch.
        """
        import json
        import uuid

        ICP = self.env["ir.config_parameter"].sudo()
        config = self._resolve_trajectory_config()

        # Gather valid instances
        valid_instances = self.instance_ids.filtered(
            lambda i: i.is_valid and i.docker_build_status == "built",
        )
        if not valid_instances:
            raise ValueError(f"No valid instances for trajectory generation in {self.name}")

        k_runs = config.get("k_runs", 8)
        total_pods = len(valid_instances) * k_runs

        self._append_log(
            f"Dispatching {total_pods} trajectory pods "
            f"({len(valid_instances)} instances x {k_runs} runs)",
        )

        # Generate unique job ID
        job_id = f"jaeger-traj-{self.org}-{self.repo_name}-{uuid.uuid4().hex[:8]}"
        self.write({
            "eks_job_id": job_id,
            "trajectory_status": "running",
            "llm_config_json": json.dumps(config),
        })

        # Create trajectory run records
        Run = self.env["jaeger.trajectory.run"]
        for inst in valid_instances:
            for run_num in range(1, k_runs + 1):
                Run.create({
                    "name": f"{inst.name}-run-{run_num}",
                    "instance_id": inst.id,
                    "repository_id": self.id,
                    "run_number": run_num,
                    "model": config.get("model_name", "claude"),
                    "status": "queued",
                    "eks_pod_name": f"{job_id}-{inst.name}-{run_num}".lower().replace("__", "-"),
                })

        self._append_log(f"Created {total_pods} trajectory run records (job_id={job_id})")

        # Dispatch to EKS
        try:
            eks_cluster = ICP.get_param("jaeger.eks_cluster", "")
            eks_namespace = ICP.get_param("jaeger.eks_namespace", "jaeger")

            if not eks_cluster:
                self._append_log("WARNING: No EKS cluster configured. Runs created but not dispatched.")
                return

            self._create_eks_jobs(config, valid_instances, k_runs, eks_cluster, eks_namespace)
            self._append_log("EKS jobs dispatched successfully")

        except Exception as e:
            self._append_log(f"EKS dispatch error: {e}")
            raise

    def _resolve_trajectory_config(self):
        """Build trajectory configuration, merging per-repo overrides with system defaults."""
        import json

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

    def _create_eks_jobs(self, config, instances, k_runs, cluster, namespace):
        """Create K8s Job manifests and submit to EKS.

        Uses the kubernetes Python client to create batch/v1 Jobs
        in the configured EKS namespace.
        """
        try:
            from kubernetes import client
            from kubernetes import config as k8s_config

            k8s_config.load_kube_config(
                config_file=os.environ.get("KUBECONFIG") or None,
            )
            batch_v1 = client.BatchV1Api()
        except ImportError:
            _logger.warning("kubernetes Python client not installed. Skipping EKS dispatch.")
            self._append_log("kubernetes client not available — runs created but not dispatched to EKS")
            return
        except Exception as e:
            _logger.warning("Could not configure K8s client: %s", e)
            self._append_log(f"K8s config error: {e} — runs created but not dispatched")
            return

        ICP = self.env["ir.config_parameter"].sudo()
        agent_image = ICP.get_param("jaeger.agent_image", "jaeger-agent:latest")

        for inst in instances:
            for run_num in range(1, k_runs + 1):
                job_name = f"{self.eks_job_id}-{inst.pr_number}-r{run_num}"
                job_name = job_name.lower().replace("__", "-")[:63]

                container = client.V1Container(
                    name="agent",
                    image=agent_image,
                    env=[
                        client.V1EnvVar(name="INSTANCE_IMAGE", value=inst.docker_image_name or ""),
                        client.V1EnvVar(name="INSTANCE_ID", value=inst.name),
                        client.V1EnvVar(name="RUN_NUMBER", value=str(run_num)),
                        client.V1EnvVar(name="MODEL_NAME", value=config.get("model_name", "")),
                        client.V1EnvVar(name="TEMPERATURE", value=str(config.get("temperature", 1.0))),
                        client.V1EnvVar(name="MAX_ITERATIONS", value=str(config.get("max_iterations", 300))),
                        client.V1EnvVar(name="TIMEOUT", value=str(config.get("conversation_timeout", 3600))),
                        client.V1EnvVar(name="WEBHOOK_URL", value=ICP.get_param("jaeger.webhook_url", "")),
                        client.V1EnvVar(name="ODOO_RECORD_ID", value=str(inst.id)),
                    ],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "1", "memory": "4Gi"},
                        limits={"cpu": "2", "memory": "8Gi"},
                    ),
                )

                job = client.V1Job(
                    metadata=client.V1ObjectMeta(
                        name=job_name,
                        namespace=namespace,
                        labels={
                            "app": "jaeger-trajectory",
                            "jaeger-job-id": self.eks_job_id or "",
                            "instance": inst.name[:63],
                        },
                    ),
                    spec=client.V1JobSpec(
                        template=client.V1PodTemplateSpec(
                            spec=client.V1PodSpec(
                                containers=[container],
                                restart_policy="Never",
                            ),
                        ),
                        backoff_limit=1,
                        active_deadline_seconds=config.get("conversation_timeout", 3600) + 300,
                    ),
                )

                try:
                    batch_v1.create_namespaced_job(namespace=namespace, body=job)
                except Exception as e:
                    _logger.error("Failed to create K8s job %s: %s", job_name, e)

    def _handle_trajectory_webhook(self, status, results):
        """Handle incoming EKS webhook with trajectory run results.

        Called by the webhook controller when an EKS pod completes.

        Args:
            status: 'completed' or 'failed'
            results: dict with run data (agent_patch, conversation, costs, etc.)
        """
        import json

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

            # Check for evaluation results
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


    def _summarize_trajectories(self):
        """Summarize trajectory results and compute pass@k."""
        import json

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

