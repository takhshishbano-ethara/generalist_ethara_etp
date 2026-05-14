# Skoll K8s Sandbox — Full Workflow Documentation

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Data Model](#data-model)
3. [Credential Flow](#credential-flow)
4. [K8s Deployment Workflow (Step-by-Step)](#k8s-deployment-workflow)
5. [K8s Resources Created](#k8s-resources-created)
6. [Container Specifications](#container-specifications)
7. [Configuration Generation](#configuration-generation)
8. [Destroy Workflow](#destroy-workflow)
9. [Reconciliation Cron](#reconciliation-cron)
10. [Dashboard URL Resolution](#dashboard-url-resolution)
11. [Comparison: Local vs K8s Mode](#comparison-local-vs-k8s-mode)
12. [Module File Map](#module-file-map)
13. [Environment Variables Reference](#environment-variables-reference)
14. [Odoo Settings Reference](#odoo-settings-reference)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                    Odoo Server                       │
│                                                     │
│  ┌──────────┐   ┌───────────────┐   ┌────────────┐ │
│  │ skoll.   │──▶│ skoll.skoll   │──▶│ skoll.     │ │
│  │ persona  │   │ (task record) │   │ sandbox.   │ │
│  │ (DB)     │   │               │   │ k8s        │ │
│  └──────────┘   └───────────────┘   └─────┬──────┘ │
│                                           │        │
│  ┌──────────┐                             │        │
│  │ .env     │─── credentials ────────────▶│        │
│  │ (file)   │                             │        │
│  └──────────┘                             │        │
└───────────────────────────────────────────┼────────┘
                                            │
                    K8s API calls            │
                                            ▼
┌─────────────────────────────────────────────────────┐
│              Kubernetes Cluster                      │
│              Namespace: ethara                       │
│                                                     │
│  ┌─────────────────────────────────────────────┐    │
│  │            Deployment: skoll-sandbox-{id}    │    │
│  │                                             │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  │    │
│  │  │ openclaw │  │ litellm  │  │ postgres │  │    │
│  │  │ :18789   │  │ :4000    │  │ :5432    │  │    │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  │    │
│  │       │              │              │        │    │
│  └───────┼──────────────┼──────────────┼────────┘    │
│          │              │              │             │
│  ┌───────┴──────┐ ┌─────┴────┐  ┌─────┴──────┐     │
│  │ ConfigMaps   │ │ Secret   │  │ PVCs       │     │
│  │ - persona    │ │ - creds  │  │ - browser  │     │
│  │ - openclaw   │ │          │  │ - db-data  │     │
│  │ - litellm    │ │          │  │            │     │
│  └──────────────┘ └──────────┘  └────────────┘     │
│                                                     │
│  ┌──────────────────┐                               │
│  │ Service (ClusterIP)                              │
│  │ skoll-sandbox-{id}                               │
│  │ port 18789 → openclaw:18789                      │
│  └──────────────────┘                               │
└─────────────────────────────────────────────────────┘
```

---

## Data Model

### skoll.persona (Database Table)

| Field                | Type    | Description                                            |
|----------------------|---------|--------------------------------------------------------|
| `name`               | Char    | Persona identifier (e.g. `marcus`). Auto-lowercased.   |
| `active`             | Boolean | Soft-delete flag.                                      |
| `soul_md`            | Text    | SOUL.md content — personal profile.                    |
| `memory_md`          | Text    | MEMORY.md content — preferences, contacts, habits.     |
| `agents_md`          | Text    | AGENTS.md content — agent behavior rules.              |
| `litellm_config_yaml`| Text   | Per-persona litellm-config.yaml override. Falls back to global default if empty. |
| `docker_compose_yaml`| Text   | Per-persona docker-compose.yml override (local mode only). Falls back to bundled default if empty. |

### skoll.skoll (Task Record)

| Field                    | Type      | Description                                     |
|--------------------------|-----------|-------------------------------------------------|
| `persona_id`             | Many2one  | Link to `skoll.persona`. Required.               |
| `docker_status`          | Selection | `stopped` / `starting` / `running` / `error`    |
| `docker_compose_project` | Char      | K8s: service name (`skoll-sandbox-{id}`)         |
| `docker_port`            | Integer   | K8s: always `18789`                              |
| `docker_gateway_token`   | Char      | 64-char hex token for gateway auth               |
| `docker_dashboard_url`   | Char      | Computed. K8s: `http://{svc}.ethara.svc.cluster.local:18789/#token={token}` |
| `docker_error`           | Text      | Error message if `docker_status == 'error'`      |

---

## Credential Flow

```
.env file (project root, gitignored)
    │
    ▼
_load_dotenv()  ←── merges with os.environ (process env takes precedence)
    │
    ├──▶ _build_openclaw_config()  →  openclaw.json ConfigMap
    │        AWS_BEARER_TOKEN_BEDROCK, BEDROCK_MODEL_ARN, AWS_REGION
    │        LITELLM_MASTER_KEY
    │
    └──▶ deploy_sandbox()  →  K8s Secret
             AWS_BEARER_TOKEN_BEDROCK, LITELLM_MASTER_KEY
             LITELLM_DB_PASSWORD, OPENCLAW_GATEWAY_TOKEN
```

### .env File Format

```bash
# Required
AWS_BEARER_TOKEN_BEDROCK=ABSKQm...
AWS_REGION=ap-south-1
BEDROCK_MODEL_ARN=arn:aws:bedrock:ap-south-1:426628337772:application-inference-profile/...

# Optional (auto-generated if missing)
LITELLM_MASTER_KEY=sk-skoll-litellm
LITELLM_DB_PASSWORD=dbpassword9090
```

### Precedence

1. Process environment variable (`export VAR=value` before starting Odoo)
2. `.env` file in the same directory as `odoo.conf`

---

## K8s Deployment Workflow

### Entry Point

```
User clicks "Start Sandbox" button in Odoo UI
    │
    ▼
skoll.skoll.action_start_sandbox()
    │
    ▼
_deployment_mode() == "k8s"  (from ir.config_parameter skoll.deployment_mode)
    │
    ▼
skoll.skoll._start_k8s()
```

### Step-by-Step Flow

#### Step 1: Generate Gateway Token

**File:** `skoll.py` → `_start_k8s()` (line 237)

```python
gateway_token = secrets.token_hex(32)  # 64-char random hex string
```

Write to task record:
```python
self.write({
    "docker_status": "starting",
    "docker_gateway_token": gateway_token,
    "docker_error": False,
})
```

#### Step 2: Call K8s Deployer

**File:** `skoll.py` → `_start_k8s()` (line 247)

```python
self.env["skoll.sandbox.k8s"].deploy_sandbox(self)
```

#### Step 3: Load Configuration

**File:** `skoll_sandbox_k8s.py` → `deploy_sandbox()` (lines 132-167)

```python
config.load_incluster_config()        # Authenticate with K8s cluster
core_v1 = client.CoreV1Api()          # For Secrets, ConfigMaps, PVCs, Services
apps_v1 = client.AppsV1Api()          # For Deployments

persona = task_record.persona_id      # skoll.persona record from DB
env = _load_dotenv()                  # Credentials from .env + os.environ

# Image URIs from Odoo settings (ir.config_parameter)
openclaw_image = "skoll.openclaw_image"   # default: ghcr.io/openclaw/openclaw:latest
litellm_image  = "skoll.litellm_image"    # default: ghcr.io/berriai/litellm:main-stable
storage_class  = "skoll.k8s_storage_class" # default: gp3
```

#### Step 4: Create K8s Secret

**File:** `skoll_sandbox_k8s.py` → `_create_secret()` (lines 240-269)

**Resource:** `Secret/skoll-sandbox-creds-{task_id}`

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: skoll-sandbox-creds-{task_id}
  namespace: ethara
  labels:
    platform: skoll
    component: sandbox
    task-id: "{task_id}"
stringData:
  OPENCLAW_GATEWAY_TOKEN: "{gateway_token}"
  LITELLM_MASTER_KEY: "{litellm_master_key}"
  LITELLM_DB_PASSWORD: "{litellm_db_password}"
  AWS_BEARER_TOKEN_BEDROCK: "{aws_bearer}"
```

#### Step 5: Create Persona ConfigMap

**File:** `skoll_sandbox_k8s.py` → `_create_persona_configmap()` (lines 271-295)

**Source:** `skoll.persona` database fields (NOT filesystem)

**Resource:** `ConfigMap/skoll-sandbox-persona-{task_id}`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: skoll-sandbox-persona-{task_id}
  namespace: ethara
data:
  SOUL.md: |
    {persona.soul_md}
  MEMORY.md: |
    {persona.memory_md}
  AGENTS.md: |
    {persona.agents_md}
```

#### Step 6: Create OpenClaw Config ConfigMap

**File:** `skoll_sandbox_k8s.py` → `_create_openclaw_config_configmap()` (lines 297-315)

**Source:** Generated by `_build_openclaw_config()` (lines 42-122)

**Resource:** `ConfigMap/skoll-sandbox-openclaw-config-{task_id}`

This bypasses the buggy `openclaw config set` CLI by pre-building the entire `openclaw.json`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: skoll-sandbox-openclaw-config-{task_id}
  namespace: ethara
data:
  openclaw.json: |
    {
      "gateway": {
        "bind": "lan",
        "auth": {"mode": "token", "token": "{gateway_token}"},
        "controlUi": {
          "allowedOrigins": ["http://localhost:18789", "http://127.0.0.1:18789", "http://0.0.0.0:18789"],
          "dangerouslyDisableDeviceAuth": true
        }
      },
      "browser": {
        "enabled": true, "headless": true, "noSandbox": true,
        "defaultProfile": "openclaw"
      },
      "models": {
        "providers": {
          "skoll-bedrock": { ... },   // Only if AWS_BEARER_TOKEN_BEDROCK + BEDROCK_MODEL_ARN set
          "litellm": {                // Always present
            "baseUrl": "http://localhost:4000/v1",
            "models": ["claude-opus-4.7", "kimi-k2.6"]
          }
        }
      },
      "agents": {"defaults": {"model": "litellm/claude-opus-4.7"}}
    }
```

**Key decisions:**
- `dangerouslyDisableDeviceAuth: true` — skips the device pairing screen
- `litellm` provider always registered (LiteLLM sidecar is always present)
- `skoll-bedrock` provider only added if AWS credentials exist in `.env`
- LiteLLM baseUrl is `http://localhost:4000/v1` (sidecar in same pod)

#### Step 7: Create LiteLLM Config ConfigMap

**File:** `skoll_sandbox_k8s.py` → `_create_litellm_configmap()` (lines 317-333)

**Source:** `persona.litellm_config_yaml` if set, otherwise `_DEFAULT_LITELLM_CONFIG` template

**Resource:** `ConfigMap/skoll-litellm-config-{task_id}`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: skoll-litellm-config-{task_id}
  namespace: ethara
data:
  config.yaml: |
    model_list:
      - model_name: claude-opus-4.7
        litellm_params:
          model: bedrock/converse/{BEDROCK_MODEL_ARN}
          aws_region_name: {AWS_REGION}
    litellm_settings:
      drop_params: true
      telemetry: false
    general_settings:
      master_key: os.environ/LITELLM_MASTER_KEY
      database_url: os.environ/DATABASE_URL
```

#### Step 8: Create PVCs

**File:** `skoll_sandbox_k8s.py` → `_create_pvc()` (lines 335-359)

Two PVCs per sandbox:

| PVC Name | Size | Purpose |
|----------|------|---------|
| `skoll-browser-{persona_name}` | 5Gi | Shared browser profiles per persona |
| `skoll-sandbox-db-{task_id}` | 2Gi | PostgreSQL data for LiteLLM |

Both use the configured `storage_class` (default: `gp3`).

**Note:** Browser profiles PVC is shared across tasks with the same persona.

#### Step 9: Create Deployment

**File:** `skoll_sandbox_k8s.py` → `_create_deployment()` (lines 361-665)

**Resource:** `Deployment/skoll-sandbox-{task_id}` with 3 containers in one pod.

See [Container Specifications](#container-specifications) below for full details.

#### Step 10: Create Service

**File:** `skoll_sandbox_k8s.py` → `_create_service()` (lines 667-695)

**Resource:** `Service/skoll-sandbox-{task_id}`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: skoll-sandbox-{task_id}
  namespace: ethara
spec:
  type: ClusterIP
  selector:
    task-id: "{task_id}"
    component: sandbox
  ports:
    - name: gateway
      port: 18789
      targetPort: 18789
```

#### Step 11: Update Odoo Record

**File:** `skoll.py` → `_start_k8s()` (lines 248-254)

```python
self.write({
    "docker_compose_project": "skoll-sandbox-{id}",  # K8s service name
    "docker_status": "starting",
    "docker_port": 18789,
})
```

Status transitions to `running` via the reconciliation cron (see below).

---

## K8s Resources Created

For a task with `id=42` and persona `marcus`:

| Resource Type | Name | Lifecycle |
|---------------|------|-----------|
| **Secret** | `skoll-sandbox-creds-42` | Per-task. Deleted on destroy. |
| **ConfigMap** | `skoll-sandbox-persona-42` | Per-task. Deleted on destroy. |
| **ConfigMap** | `skoll-sandbox-openclaw-config-42` | Per-task. Deleted on destroy. |
| **ConfigMap** | `skoll-litellm-config-42` | Per-task. Deleted on destroy. |
| **PVC** | `skoll-browser-marcus` | Per-persona. **NOT** deleted on destroy (shared). Labels: `platform: skoll`, `component: browser-profiles`, `persona: {name}` |
| **PVC** | `skoll-sandbox-db-42` | Per-task. Deleted on destroy. |
| **Deployment** | `skoll-sandbox-42` | Per-task. Deleted on destroy. |
| **Service** | `skoll-sandbox-42` | Per-task. Deleted on destroy. |

All resources except the browser PVC are created in namespace `ethara` with labels:
```yaml
platform: skoll
component: sandbox
task-id: "42"
app.kubernetes.io/name: skoll-sandbox
app.kubernetes.io/managed-by: skoll-odoo
```

---

## Container Specifications

### Pod: 3 Containers (sidecar pattern)

All containers share `localhost` networking within the pod.

### 1. OpenClaw Container

| Property | Value |
|----------|-------|
| **Image** | `skoll.openclaw_image` setting (default: `ghcr.io/openclaw/openclaw:latest`) |
| **Command** | `["node", "openclaw.mjs", "gateway", "--allow-unconfigured", "--token", "{gateway_token}"]` |
| **Port** | 18789 |
| **CPU** | request: 1, limit: 2 |
| **Memory** | request: 2Gi, limit: 4Gi |

**Volume Mounts:**

| Volume | Mount Path | Description |
|--------|------------|-------------|
| `persona-files` (ConfigMap) | `/sandbox/personas/{persona}` | SOUL.md, MEMORY.md, AGENTS.md (read-only) |
| `browser-profiles` (PVC) | `/home/node/.openclaw/browser-profiles` | Persistent browser data |
| `openclaw-data` (EmptyDir) | `/home/node/.openclaw` | Ephemeral workspace |
| `openclaw-config` (ConfigMap) | `/home/node/.openclaw/openclaw.json` (subPath) | Pre-built config (read-only) |

**Probes:**

| Probe | Endpoint | Timing |
|-------|----------|--------|
| Startup | `GET /healthz :18789` | delay=10s, period=5s, failures=30 (150s max) |
| Readiness | `GET /healthz :18789` | period=10s, timeout=5s |
| Liveness | `GET /healthz :18789` | delay=60s, period=15s, timeout=5s |

**Environment Variables:**

| Variable | Source |
|----------|--------|
| `OPENCLAW_GATEWAY_TOKEN` | Secret (key ref) |
| `AWS_BEARER_TOKEN_BEDROCK` | Secret (key ref) |
| `LITELLM_MASTER_KEY` | Secret (key ref) |
| `PERSONA` | Direct value |
| `HOME` | `/home/node` |
| `TERM` | `xterm-256color` |
| `PLAYWRIGHT_BROWSERS_PATH` | `/home/node/.cache/ms-playwright` |
| `AWS_REGION` | Direct value |
| `BEDROCK_MODEL_ARN` | Direct value |

**Why `command` overrides the entrypoint:**

The base OpenClaw Docker image has an entrypoint (`skoll-entrypoint.sh`) that calls `openclaw config set` and `openclaw devices clear`. Both of these CLI commands have a known bug where they spin-lock at 99% CPU indefinitely. By setting `command`, we bypass the entrypoint entirely and start the gateway binary directly. The pre-built `openclaw.json` ConfigMap provides all configuration the entrypoint would have generated.

### 2. LiteLLM Container

| Property | Value |
|----------|-------|
| **Image** | `skoll.litellm_image` setting (default: `ghcr.io/berriai/litellm:main-stable`) |
| **Command** | `["--config", "/app/config.yaml", "--port", "4000"]` |
| **Port** | 4000 (not exposed outside pod) |
| **CPU** | request: 500m, limit: 1 |
| **Memory** | request: 512Mi, limit: 2Gi |

**Volume Mounts:**

| Volume | Mount Path | Description |
|--------|------------|-------------|
| `litellm-config` (ConfigMap) | `/app/config.yaml` (subPath) | LiteLLM routing config |

**Probes:**

| Probe | Endpoint | Timing |
|-------|----------|--------|
| Readiness | `exec: python3 urllib.request.urlopen('http://localhost:4000/health/liveliness')` | period=15s, timeout=10s, failures=5 |

**Environment Variables:**

| Variable | Source |
|----------|--------|
| `LITELLM_MASTER_KEY` | Secret (key ref) |
| `AWS_BEARER_TOKEN_BEDROCK` | Secret (key ref) |
| `LITELLM_DB_PASSWORD` | Secret (key ref) |
| `DATABASE_URL` | Direct: `postgresql://llmproxy:{password}@localhost:5432/litellm` |
| `STORE_MODEL_IN_DB` | `"True"` |
| `AWS_REGION` | Direct value |

### 3. PostgreSQL Container

| Property | Value |
|----------|-------|
| **Image** | `postgres:16` |
| **Port** | 5432 (not exposed outside pod) |
| **CPU** | request: 250m, limit: 500m |
| **Memory** | request: 256Mi, limit: 512Mi |

**Volume Mounts:**

| Volume | Mount Path | Description |
|--------|------------|-------------|
| `db-data` (PVC) | `/var/lib/postgresql/data` | Persistent database storage |

**Probes:**

| Probe | Endpoint | Timing |
|-------|----------|--------|
| Readiness | `exec: pg_isready -d litellm -U llmproxy` | delay=5s, period=5s, timeout=5s |
| Liveness | `exec: pg_isready -d litellm -U llmproxy` | delay=30s, period=10s |

---

## Configuration Generation

### openclaw.json (built by `_build_openclaw_config()`)

```
_load_dotenv()
    │
    ├─ AWS_BEARER_TOKEN_BEDROCK ─┐
    ├─ BEDROCK_MODEL_ARN ────────┤    if both set → skoll-bedrock provider
    ├─ AWS_REGION ───────────────┘
    │
    ├─ LITELLM_MASTER_KEY ───────────→ litellm provider (always)
    │
    └─ gateway_token (from _start_k8s) → auth.token
```

### litellm-config.yaml

```
persona.litellm_config_yaml  (DB field)
    │
    ├─ if set → use persona's custom config
    │
    └─ if empty → _DEFAULT_LITELLM_CONFIG template
                      │
                      ├─ {bedrock_arn} from .env BEDROCK_MODEL_ARN
                      └─ {aws_region} from .env AWS_REGION
```

---

## Destroy Workflow

### Entry Point

```
User clicks "Stop Sandbox"
    │
    ▼
skoll.skoll.action_stop_sandbox()
    │
    ▼
_deployment_mode() == "k8s"
    │
    ▼
skoll.skoll._stop_k8s()
    │
    ▼
skoll.sandbox.k8s.destroy_sandbox(task_record)
```

### Resources Deleted (in order)

**File:** `skoll_sandbox_k8s.py` → `destroy_sandbox()` (lines 697-742)

1. `Deployment/skoll-sandbox-{id}`
2. `Service/skoll-sandbox-{id}`
3. `Secret/skoll-sandbox-creds-{id}`
4. `ConfigMap/skoll-sandbox-persona-{id}`
5. `ConfigMap/skoll-sandbox-openclaw-config-{id}`
6. `ConfigMap/skoll-litellm-config-{id}`
7. `PVC/skoll-sandbox-db-{id}`

**NOT deleted:** `PVC/skoll-browser-{persona_name}` (shared across tasks with same persona).

All deletions use `_delete_resource()` which silently ignores `404 Not Found` errors (idempotent).

### Odoo Record Reset

```python
self.write({
    "docker_compose_project": False,
    "docker_status": "stopped",
    "docker_port": 0,
    "docker_litellm_port": 0,
    "docker_gateway_token": False,
    "docker_error": False,
})
```

---

## Reconciliation Cron

**File:** `skoll.py` → `_cron_reconcile_sandboxes()` (line 218)

**Schedule:** Every 1 minute (configured in `data/cron.xml`)

**Purpose:** Poll K8s for actual deployment status and sync with Odoo records.

### Flow

```
Cron fires
    │
    ▼
Search all tasks with docker_status in ["starting", "running"]
    │
    ▼
For each task → get_sandbox_status(task)
    │
    ├─ Deployment has available_replicas >= 1 → "running"
    ├─ Deployment has replicas > 0 → "starting"
    ├─ Deployment not found + status was "starting" + >300s → "error"
    └─ Deployment not found → "stopped"
    │
    ▼
Update task.docker_status if changed
```

This is how `docker_status` transitions from `starting` → `running` in K8s mode (there's no synchronous health-check wait like in local mode).

---

## Dashboard URL Resolution

**File:** `skoll.py` → `_compute_dashboard_url()` (line 180)

### K8s Mode

```
http://{service_name}.ethara.svc.cluster.local:18789/#token={gateway_token}
```

Example: `http://skoll-sandbox-42.ethara.svc.cluster.local:18789/#token=abc123...`

This URL is accessible within the K8s cluster. For external access, an Ingress or port-forward is needed.

### Local Mode (for comparison)

```
http://localhost:{dynamic_port}/#token={gateway_token}
```

Example: `http://localhost:19042/#token=abc123...`

---

## Comparison: Local vs K8s Mode

| Aspect | Local (Docker Compose) | K8s |
|--------|------------------------|-----|
| **Trigger** | `_start_local()` | `_start_k8s()` |
| **Infrastructure** | Docker daemon + compose | K8s cluster (in-cluster config) |
| **Port** | Dynamic: `19000 + (id % 1000)` | Fixed: `18789` (ClusterIP) |
| **Multi-user** | Dynamic ports avoid collision | Each task = separate Deployment |
| **Persona source** | `skoll.persona` DB fields | `skoll.persona` DB fields |
| **Credentials** | `.env` file → `os.environ` | `.env` file → K8s Secret |
| **openclaw.json** | Written to temp workdir filesystem | ConfigMap mounted into pod |
| **litellm-config** | Written to temp workdir filesystem | ConfigMap mounted into pod |
| **Entrypoint bypass** | compose override: `entrypoint: [...]` | Container `command: [...]` |
| **Health check** | Python polling loop (120s timeout) | K8s startup/readiness probes |
| **Status sync** | Synchronous (blocks until healthy) | Async (cron reconciliation) |
| **Cleanup** | `docker compose down` + `shutil.rmtree` | Delete K8s resources |
| **Network** | Host network (localhost:port) | K8s ClusterIP Service |
| **Docker image** | Built from bundled Dockerfile | Pulled from registry (Odoo setting) |

---

## Module File Map

```
custom_addons/skoll/
├── __init__.py
├── __manifest__.py                    # v19.0.5.0.0
├── controllers/
│   └── llm_assisst_qc.py
├── data/
│   ├── cron.xml                       # Reconciliation cron (1 min)
│   └── persona_seed.xml               # Marcus seed data
├── models/
│   ├── __init__.py
│   ├── res_config_settings.py         # Odoo Settings page fields
│   ├── skoll.py                       # Main model + _load_dotenv + local mode
│   ├── skoll_domain.py                # Legacy domain model
│   ├── skoll_persona.py               # Persona model (DB-driven)
│   └── skoll_sandbox_k8s.py           # K8s deployer
├── sandbox_docker/                    # Bundled Docker files (local mode)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── litellm-patch-entrypoint.sh
├── security/
│   ├── ir.model.access.csv
│   └── skoll_security.xml
├── views/
│   ├── domain_views.xml
│   ├── menuitems.xml
│   ├── persona_views.xml
│   ├── res_config_settings_views.xml
│   └── skoll_views.xml
└── .env                               # Credentials (gitignored, project root)
```

---

## Environment Variables Reference

### Required in `.env`

| Variable | Example | Used By |
|----------|---------|---------|
| `AWS_BEARER_TOKEN_BEDROCK` | `ABSKQm...` | openclaw.json (skoll-bedrock provider), K8s Secret |
| `AWS_REGION` | `ap-south-1` | openclaw.json, litellm-config, K8s container env |
| `BEDROCK_MODEL_ARN` | `arn:aws:bedrock:...` | openclaw.json (skoll-bedrock provider), litellm-config template |

### Optional in `.env` (auto-generated if missing)

| Variable | Default | Used By |
|----------|---------|---------|
| `LITELLM_MASTER_KEY` | `sk-skoll-{random}` | openclaw.json (litellm provider apiKey), K8s Secret |
| `LITELLM_DB_PASSWORD` | `{random_hex}` | K8s Secret, PostgreSQL POSTGRES_PASSWORD |

---

## Odoo Settings Reference

### K8s-Specific Settings (ir.config_parameter)

| Key | Default | Description |
|-----|---------|-------------|
| `skoll.deployment_mode` | `local` | `"k8s"` to enable K8s mode |
| `skoll.openclaw_image` | `ghcr.io/openclaw/openclaw:latest` | OpenClaw container image URI |
| `skoll.litellm_image` | `ghcr.io/berriai/litellm:main-stable` | LiteLLM container image URI |
| `skoll.k8s_storage_class` | `gp3` | StorageClassName for PVCs |

These are configured in **Settings → Skoll → Kubernetes Mode**.

### Node Selector (hardcoded)

```python
NODE_SELECTOR = {
    "kubernetes.io/arch": "amd64",
    "ethara.ai/node-pool": "general-purpose",
}
```

Pods are scheduled only on amd64 nodes in the `general-purpose` node pool.
