"""Kubernetes worker-Deployment auto-scaler for Leviathan PRD workers.

Ported from ``custom_addons/vegeta/models/vegeta_job.py`` (the
``_run_worker_deployment_scaler`` family) so Leviathan reaches v19.0.2.6.0
parity for stage rollout.

Why a separate module:
    The K8s python client is an optional dependency — only the production
    Odoo backend pod has it. Local dev (``prd_execution_mode=inprocess``)
    and the standalone worker container do not need it. Isolating the
    import here keeps ``leviathan_job.py`` importable without
    ``kubernetes`` installed; the model dispatches into this module only
    when the scaler cron actually fires.

Public API:
    ``K8S_AVAILABLE`` — bool guard, mirrors the import success
    ``worker_scaler_config(env)`` — reads ``ir.config_parameter`` values
    ``run_worker_deployment_scaler(env, load)`` — patches the Deployment

The model side keeps ownership of:
    * the advisory lock ID (so all dispatch-side locks stay together)
    * the LOAD calculation (a SQL ``search_count`` on the queue states
      that count against scaler load — this differs subtly between
      Vegeta and Leviathan because Leviathan's queue states are
      ``generating`` + the started-but-unfinished rows, and we filter
      via the same admission expression)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime as _dt

from odoo import fields as _odoo_fields

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional kubernetes import — must not break addons load on dev images
# that don't ship the client.
# ---------------------------------------------------------------------------
try:
    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.client.rest import ApiException as K8sApiException  # noqa: F401
    K8S_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised on dev / worker pod
    k8s_client = None  # type: ignore[assignment]
    k8s_config = None  # type: ignore[assignment]
    K8sApiException = Exception  # type: ignore[assignment,misc]
    K8S_AVAILABLE = False


# Cluster config is loaded once and refreshed lazily — IRSA tokens behind
# load_incluster_config rotate, so the cache is bounded ~50 min.
_k8s_config_lock = threading.Lock()
_k8s_config_loaded = False
_k8s_config_loaded_at = 0.0
_K8S_CONFIG_MAX_AGE = 3000


def _load_k8s_config():
    """Idempotent, time-bounded load of in-cluster or kubeconfig auth."""
    global _k8s_config_loaded, _k8s_config_loaded_at
    if _k8s_config_loaded and (time.time() - _k8s_config_loaded_at) < _K8S_CONFIG_MAX_AGE:
        return
    with _k8s_config_lock:
        if _k8s_config_loaded and (time.time() - _k8s_config_loaded_at) < _K8S_CONFIG_MAX_AGE:
            return
        try:
            k8s_config.load_incluster_config()
        except Exception:
            # Fall back to ~/.kube/config for local / dev / non-K8s shells.
            k8s_config.load_kube_config()
        _k8s_config_loaded = True
        _k8s_config_loaded_at = time.time()


# ---------------------------------------------------------------------------
# Settings lookup — all keys live under ``leviathan.*`` for symmetry with
# the existing Settings page; defaults match vegeta v19.0.2.6.0.
# ---------------------------------------------------------------------------
def worker_scaler_config(env):
    ICP = env["ir.config_parameter"].sudo()
    return {
        "deployment_name": ICP.get_param(
            "leviathan.worker_deployment_name", "leviathan-prd-worker",
        ),
        "namespace": ICP.get_param("leviathan.k8s_namespace", "leviathan"),
        "min_replicas": int(ICP.get_param("leviathan.worker_min_replicas", "1")),
        "max_replicas": int(ICP.get_param("leviathan.worker_max_replicas", "10")),
        "per_pod_concurrency": int(
            ICP.get_param("leviathan.worker_target_concurrency", "100"),
        ),
        "scale_down_cooldown_s": int(
            ICP.get_param("leviathan.worker_scale_down_cooldown_s", "600"),
        ),
    }


# ---------------------------------------------------------------------------
# Scaler body
# ---------------------------------------------------------------------------
def run_worker_deployment_scaler(env, load: int) -> None:
    """Patch the worker Deployment's replica count.

    ``load`` is the model's authoritative count of work that should
    pressure scale-up: typically ``COUNT(leviathan_job WHERE state in
    ('generating','scoring','qc_running') AND cancel_requested=false)``,
    computed by the caller. The caller passes it in so this module
    stays free of model knowledge.

    Hysteresis is asymmetric (port of vegeta H2):
      * scale-up — immediate (burst response, no cooldown)
      * scale-down — gated by ``worker_scale_down_cooldown_s`` since the
        last successful patch (tracked in ICP
        ``leviathan.worker_last_scale_change_utc``)

    Silent no-ops:
      * K8s python client not installed → log warning, return
      * Deployment read fails → log exception, return (don't patch blind)
      * current == desired → debug log, return
    """
    if not K8S_AVAILABLE:
        _logger.warning(
            "[leviathan] worker scaler: kubernetes package unavailable — "
            "Deployment cannot be scaled from Odoo. Set "
            "leviathan.prd_execution_mode=inprocess for local dev, or "
            "install the kubernetes package in the Odoo backend image."
        )
        return

    cfg = worker_scaler_config(env)

    # Math:  desired = clamp(ceil(load / per_pod), min, max)
    #        ceil(a/b) via -(-a // b) — integer-only, avoids float drift.
    per_pod = max(1, cfg["per_pod_concurrency"])
    desired = -(-max(load, 0) // per_pod)
    desired = max(cfg["min_replicas"], min(desired, cfg["max_replicas"]))

    try:
        _load_k8s_config()
        apps_v1 = k8s_client.AppsV1Api()
        deployment = apps_v1.read_namespaced_deployment(
            name=cfg["deployment_name"], namespace=cfg["namespace"],
        )
    except Exception:
        _logger.exception(
            "[leviathan] worker scaler: failed to read Deployment %s/%s",
            cfg["namespace"], cfg["deployment_name"],
        )
        return

    # H1+H10 (vegeta): max(spec, status). During a drain, spec=desired
    # while status.replicas is the pod count still alive; treating spec
    # alone as "current" would prompt a no-op while pods are still
    # finishing — burning compute on the next burst.
    spec_replicas = (deployment.spec.replicas if deployment.spec else 0) or 0
    status_replicas = (
        deployment.status.replicas if deployment.status else 0
    ) or 0
    current = max(spec_replicas, status_replicas)

    if current == desired:
        _logger.debug(
            "[leviathan] worker scaler: %s/%s already at %d "
            "(spec=%d status=%d, load=%d)",
            cfg["namespace"], cfg["deployment_name"], current,
            spec_replicas, status_replicas, load,
        )
        return

    # Scale-down hysteresis. Scale-up is immediate; scale-down waits for
    # the cooldown to elapse so a burst+drain cycle doesn't flap.
    if desired < current:
        ICP = env["ir.config_parameter"].sudo()
        cooldown_s = cfg["scale_down_cooldown_s"]
        last_change = ICP.get_param("leviathan.worker_last_scale_change_utc", "")
        if last_change:
            try:
                last_dt = _dt.fromisoformat(last_change)
                elapsed = (_odoo_fields.Datetime.now() - last_dt).total_seconds()
                if elapsed < cooldown_s:
                    _logger.info(
                        "[leviathan] worker scaler: load=%d desired=%d < "
                        "current=%d (spec=%d status=%d), scale-down "
                        "cooldown active (elapsed=%.0fs / %ds) — deferring",
                        load, desired, current, spec_replicas,
                        status_replicas, elapsed, cooldown_s,
                    )
                    return
            except Exception:
                _logger.warning(
                    "[leviathan] worker scaler: could not parse "
                    "last-scale timestamp %r — proceeding with scale-down "
                    "(hysteresis effectively disabled this tick)",
                    last_change,
                )

    _logger.info(
        "[leviathan] worker scaler: %s/%s %d -> %d replicas "
        "(spec=%d status=%d load=%d per_pod=%d range=%d..%d)",
        cfg["namespace"], cfg["deployment_name"], current, desired,
        spec_replicas, status_replicas, load,
        per_pod, cfg["min_replicas"], cfg["max_replicas"],
    )

    try:
        # Strategic-merge patch with just spec.replicas — canonical scale
        # path in the kubernetes Python client. Avoids V1Scale shape bugs.
        apps_v1.patch_namespaced_deployment(
            name=cfg["deployment_name"],
            namespace=cfg["namespace"],
            body={"spec": {"replicas": desired}},
        )
        env["ir.config_parameter"].sudo().set_param(
            "leviathan.worker_last_scale_change_utc",
            _odoo_fields.Datetime.now().isoformat(),
        )
    except Exception:
        _logger.exception(
            "[leviathan] worker scaler: patch %s/%s -> %d replicas failed",
            cfg["namespace"], cfg["deployment_name"], desired,
        )
