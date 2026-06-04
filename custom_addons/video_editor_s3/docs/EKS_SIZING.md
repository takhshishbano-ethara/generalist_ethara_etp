# EKS Pod Sizing — Crowley Sourcing (video_editor_s3)

Concrete, conservative sizing for the Odoo deployment hosting the
Crowley Sourcing addon. Fits the constraints: min 0 pods, max 10 pods,
4–6 GB RAM per pod, ~500 active users.

## Recommended pod spec

```yaml
resources:
  requests:
    memory: "4Gi"
    cpu: "1"
  limits:
    memory: "6Gi"
    cpu: "2"
```

## Odoo config (env or odoo.conf)

```
workers = 6
max_cron_threads = 1
db_maxconn = 12
limit_memory_soft = 671088640   # 640 MB per worker
limit_memory_hard = 838860800   # 800 MB per worker
limit_request = 8192
limit_time_cpu = 600
limit_time_real = 1800          # 30 min — covers Gemini reasoning_effort=high
```

Plus the addon-level env:

```
VIDEO_EDITOR_S3_MAX_WORKERS = 2
VIDEO_EDITOR_S3_MAX_CONCURRENT = 2
```

## Why these numbers

### Why 6 workers, not 8

Each Odoo worker holds Python + Odoo core + Crowley Sourcing addon
≈ 400 MB resident. During an LLM QC job, the worker buffers the base64
video (~67 MB for a 50 MB clip) for the duration of the OpenRouter call
(60–180s). With `limit_memory_soft = 640 MB`, peak per worker is ~700 MB.

- 8 workers × 700 MB = **5.6 GB** → too close to the 6 GB limit, OOM
  risk on cron/HTTP overlap
- 6 workers × 700 MB = **4.2 GB** → fits inside 4 GB request with
  ~1.8 GB headroom in the 6 GB limit

Headroom matters because Odoo workers occasionally spike to 1 GB on
large RPC payloads (exports, bulk imports).

### Why `VIDEO_EDITOR_S3_MAX_CONCURRENT = 2`

Each worker's `ThreadPoolExecutor` runs at most 2 LLM QC / render /
export jobs simultaneously. With 6 workers per pod × 2 = 12 in-flight
jobs per pod. Going to 4 per worker would put 4 × 67 MB = 268 MB of
video buffer in each worker, pushing past the soft limit.

### Why `db_maxconn = 12` per worker

- 6 workers × 12 = 72 connections per pod
- 10 pods × 72 = 720 connections max to Postgres

That's safe for db.t3.medium (~410 max) and comfortable for db.t3.large
(~1683 max). Drop to `db_maxconn = 8` if you're on db.t3.small.

## Cluster concurrency at peak (10 pods)

| Workload                  | Capacity                              |
| ------------------------- | ------------------------------------- |
| HTTP requests / sec       | 60 workers × ~10 req/s each ≈ 600 r/s |
| Concurrent LLM QC jobs    | 60 workers × 2 = **120 in-flight**    |
| Concurrent video renders  | shared pool, same 120 ceiling         |

For 500 users, with most idle and ~10% actively running QC at any
moment, that's 50 concurrent QC jobs — well under the 120 ceiling.

## On `minReplicas: 0`

**Recommended: `minReplicas: 1` instead of 0.**

Reasons:

1. Odoo cold start = 30–60s (load addons, initialize registry, attach
   to Postgres). First user after scale-to-zero waits ~45s — bad UX.
2. Long-polling websocket bus (chatter notifications) keeps idle
   connections; scaling to zero drops them, frontend tries to
   reconnect, looks like an outage to users.
3. EKS pod scheduling + image pull for cold spin-up adds another
   20–40s.

If cost is the driver, `minReplicas: 1` + node-level scaling
(cluster-autoscaler) gives you the same savings overnight without the
cold-start hit.

If you must use 0, set `terminationGracePeriodSeconds: 60` so in-flight
LLM QC jobs (up to 3 min) can finish via SIGTERM handling, and accept
that the first user request after scale-up waits.

## HPA config

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ethara-odoo
spec:
  minReplicas: 1     # change to 0 if you really must
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 75
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 600   # 10 min — Odoo is sticky
    scaleUp:
      stabilizationWindowSeconds: 60
      policies:
        - type: Pods
          value: 2
          periodSeconds: 60
```

10-min scale-down stabilization is important because Odoo sessions are
sticky and LLM QC jobs take minutes — you don't want a pod yanked
mid-job.

## Final answer in one line

**6 workers per pod × 2 concurrent jobs × 1–10 pods → up to 120
concurrent LLM QC jobs cluster-wide, fits comfortably in 4–6 GB RAM
per pod, supports ~500 active users.**

If you need higher throughput later: bump to 8 workers and 8 GB pods
(the memory math doesn't fit at 6 GB).
