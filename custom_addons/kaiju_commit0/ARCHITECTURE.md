# kaiju_commit0 — Odoo Module Architecture

Current state as of 2026-05-14 (post workflow-logs feature).

---

## 1. Purpose

Odoo-facing control plane for the **Kaiju AI Coding Benchmark** pipeline.
Users create **Builds** (one per target repository) and **Runs** (one per LLM
model evaluation against a built image). The module submits Argo Workflows
to an EKS cluster, polls/receives status updates, and surfaces per-step pod
logs inside the Odoo UI — even after Argo's pod garbage collection.

---

## 2. Module Layout

```
kaiju_commit0/
├── __manifest__.py                     # Module manifest (deps: base, web)
├── __init__.py
├── controllers/
│   └── main.py                         # Webhook receiver (build + run callbacks)
├── data/
│   ├── ir_config_parameter_data.xml    # Argo URL, namespace, tokens
│   ├── ir_cron_data.xml                # 2 cron jobs (build poll, run poll)
│   └── ir_sequence_data.xml            # Build/Run ID sequences
├── models/
│   ├── argo_client.py                  # AbstractModel — HTTP client for Argo
│   ├── kaiju_commit0.py                # Build record + cron + step sync/persist
│   ├── kaiju_commit0_run.py            # Run record + cron + step sync/persist
│   ├── kaiju_workflow_step.py          # Per-step pod log + status (NEW)
│   └── res_config_settings.py          # Settings UI binding
├── security/
│   ├── ir.model.access.csv             # ACLs (build, run, step, wizard)
│   └── kaiju_commit0_security.xml      # Security groups
├── static/src/components/
│   ├── phase_stepper/                  # OWL widget — phase navigation
│   └── terminal_viewer/                # OWL widget — terminal-style log
├── views/
│   ├── kaiju_commit0_views.xml         # Build list + form
│   ├── kaiju_commit0_run_views.xml     # Run list + form
│   ├── kaiju_workflow_step_views.xml   # Step list + form (NEW)
│   ├── kaiju_commit0_menus.xml         # Menu structure
│   └── res_config_settings_views.xml   # Settings form (Argo config)
└── wizard/
    └── import_csv_wizard.py            # Bulk-import builds from CSV
```

---

## 3. Domain Model (ERD)

```mermaid
erDiagram
    KAIJU_COMMIT0 ||--o{ KAIJU_COMMIT0_RUN : has_runs
    KAIJU_COMMIT0 ||--o{ KAIJU_WORKFLOW_STEP : has_build_steps
    KAIJU_COMMIT0_RUN ||--o{ KAIJU_WORKFLOW_STEP : has_run_steps

    KAIJU_COMMIT0 {
        char name PK
        char repo_name
        selection language
        char branch_name
        selection current_phase
        boolean config_valid
        selection build_status
        datetime build_start
        datetime build_end
        text build_log
        char image_uri
        char s3_dataset_uri
        char workflow_name
    }

    KAIJU_COMMIT0_RUN {
        char name PK
        many2one build_id FK
        selection model_name
        integer num_samples
        integer max_iteration
        boolean use_spec_info
        selection run_status
        datetime run_start
        datetime run_end
        text run_log
        char workflow_name
        float pass_rate
        integer tests_passed
        integer tests_failed
        integer tests_total
        float duration_seconds
        float cost_usd
        integer tokens_input
        integer tokens_output
    }

    KAIJU_WORKFLOW_STEP {
        char node_id
        char display_name
        char pod_name
        char template_name
        char node_type
        selection phase
        text message
        text log_text
        datetime log_fetched_at
        datetime started_at
        datetime finished_at
        many2one build_id FK
        many2one run_id FK
    }
```

**Constraints**:

- `kaiju.commit0.workflow.step` has a SQL CHECK enforcing exactly one of `build_id`/`run_id` is set
- Composite uniques: `(build_id, node_id)` and `(run_id, node_id)` — a node id is unique within a workflow

---

## 4. Configuration (`ir.config_parameter`)

| Key | Default | Used By |
|---|---|---|
| `kaiju.argo_server_url` | `https://argo-workflows-server.argo.svc.cluster.local:2746` | `ArgoClient._get_config()` |
| `kaiju.argo_namespace` | `argo` | `ArgoClient` (all endpoints) |
| `kaiju.argo_token_path` | `/var/run/secrets/kubernetes.io/serviceaccount/token` | `ArgoClient._get_token()` (in-cluster SA token) |
| `kaiju.argo_verify_tls` | `false` | `ArgoClient` (SSL context) |
| `kaiju.odoo_internal_url` | `http://odoo-web.odoo.svc:8069` | Build/Run callback URL builder |
| `kaiju.webhook_token` | `CHANGE-ME-...` | `KaijuCallbackController._validate_token()` |

All parameters editable via **Settings → Kaiju** (`res_config_settings.py` exposes them as transient fields).

---

## 5. Argo HTTP Client (`kaiju.argo.client`)

`AbstractModel` using **`urllib.request`** (no `requests` dep). Single internal
helper `_request(method, path, body)` returns parsed JSON; **`_request_stream`**
(added with logs feature) returns raw response text for SSE endpoints.

| Method | HTTP | Purpose |
|---|---|---|
| `submit_workflow(template, params, labels)` | `POST /api/v1/workflows/{ns}/submit` | Submit `WorkflowTemplate` |
| `get_workflow_status(name)` | `GET /api/v1/workflows/{ns}/{name}` | Returns phase + progress + nodes |
| `list_workflow_nodes(name)` | `GET /api/v1/workflows/{ns}/{name}` | Filters `status.nodes` to Pod-type only |
| `get_pod_logs(name, pod, container, tail_lines)` | `GET /api/v1/workflows/{ns}/{name}/log` | SSE-parsed plain text |
| `stop_workflow(name, msg)` | `PUT /api/v1/workflows/{ns}/{name}/stop` | Graceful stop |
| `terminate_workflow(name)` | `PUT /api/v1/workflows/{ns}/{name}/terminate` | Kill all pods |

**Auth**: `Authorization: Bearer <SA-token-from-disk>`.
**TLS**: Configurable verify; defaults to disabled (in-cluster self-signed cert).
**Timeout**: 30s default, 60s for log endpoint.
**Errors**: Wraps `HTTPError`/`URLError` as `RuntimeError` (callers catch this).

---

## 6. Webhook Controller (`/kaiju/callback/*`)

Two `http.route` endpoints, both `type="http"`, `auth="none"`, `csrf=False`,
bearer-token-authenticated.

| Route | Triggered By | Payload Fields | Effect |
|---|---|---|---|
| `POST /kaiju/callback/build` | `kaiju-build-pipeline` workflow last step | `job_id, status, image_uri, s3_dataset_uri, message?` | Sets `build_status` + outputs |
| `POST /kaiju/callback/run` | `kaiju-run-pipeline` workflow last step | `job_id, status, pass_rate, tests_*, duration_seconds, cost_usd, tokens_*` | Sets `run_status` + metrics |

**Dual-update pattern**: callbacks are the **primary** completion signal; the
1-minute cron poll is the **fallback** (works even if callback can't reach
Odoo from cluster).

---

## 7. Cron Jobs (every 1 min)

```mermaid
sequenceDiagram
    participant Cron as ir.cron
    participant Build as kaiju.commit0
    participant Argo as ArgoClient
    participant Step as kaiju.commit0.workflow.step

    Cron->>Build: _cron_poll_build_status()
    Build->>Build: search running builds
    loop For each running build
        Build->>Argo: get_workflow_status(wf)
        Argo-->>Build: {phase, progress, nodes}
        Note over Build: capture previous_status
        Build->>Build: _sync_steps()
        Build->>Argo: list_workflow_nodes(wf)
        Argo-->>Build: [{id, displayName, phase, ...}]
        loop For each Pod node
            Build->>Step: upsert by (build_id, node_id)
        end
        Build->>Build: update status (done/failed/running)
        alt running → terminal transition
            Build->>Build: _persist_step_logs()
            loop For each Pod step
                Build->>Argo: get_pod_logs(wf, pod_name)
                Argo-->>Step: SSE-parsed log text
                Step->>Step: write log_text + log_fetched_at
            end
        end
    end
```

Both cron jobs follow the same pattern (build vs run). The
running→terminal transition is detected by capturing `previous_status` before
the write and comparing afterward, so logs are persisted **exactly once** —
avoiding repeated Argo log API hits after completion.

---

## 8. View / UI Architecture

```mermaid
graph TB
    Menu["Menu: Kaiju Commit0"] --> Builds["Builds (list/form)"]
    Menu --> Runs["Runs (list/form)"]
    Menu --> Settings["Settings"]
    Builds -- "click row" --> BuildForm["Build Form<br/>(2-phase stepper)"]
    BuildForm --> Config["Screen 1: Config<br/>(repo, language, branch)"]
    BuildForm --> Build2["Screen 2: Build<br/>(submit + build_log terminal)"]
    Build2 --> StepsBuild["Workflow Steps section<br/>(One2many inline list)"]
    StepsBuild -- "click step" --> StepForm["Step Form<br/>(terminal log + refresh)"]
    Runs -- "click row" --> RunForm["Run Form<br/>(config + run_log terminal)"]
    RunForm --> StepsRun["Workflow Steps section<br/>(One2many inline list)"]
    StepsRun -- "click step" --> StepForm
    Builds -- "Import CSV" --> Wizard["Import Wizard<br/>(bulk-create builds)"]
```

**Custom OWL widgets** (loaded via `assets_backend`):

- `commit0_stepper` — phase navigation header on build form
- `commit0_terminal` — terminal-style renderer for any `Text` field. Used by `build_log`, `run_log`, and step `log_text`. Features: line-by-line reveal, error/warning colorization, copy button, light/dark toggle.

---

## 9. End-to-End Flow

### Build flow (prepare → image build → callback)

```mermaid
sequenceDiagram
    participant User
    participant Odoo as Odoo (kaiju.commit0)
    participant Argo as Argo Server
    participant Pipeline as Build Workflow

    User->>Odoo: action_validate_config()
    User->>Odoo: action_run_build()
    Odoo->>Argo: submit_workflow("kaiju-build-pipeline", params)
    Argo-->>Odoo: workflow_name
    Odoo->>Odoo: build_status = 'running'
    Pipeline->>Pipeline: prepare (clone, stub, scrape spec, upload S3)
    Pipeline->>Pipeline: build-repo-image (kaniko → ECR)
    Pipeline->>Odoo: POST /kaiju/callback/build {status, image_uri, s3_dataset_uri}
    Odoo->>Odoo: build_status = 'done', store outputs
    Note over Odoo: Concurrently, cron polls Argo every 60s as fallback
```

### Run flow (3-stage agentic eval → finalize → callback)

```mermaid
sequenceDiagram
    participant User
    participant Odoo as Odoo (kaiju.commit0.run)
    participant Argo as Argo Server
    participant Pipeline as Run Workflow

    User->>Odoo: action_create_run() (from completed build)
    User->>Odoo: action_run()
    Odoo->>Argo: submit_workflow("kaiju-run-pipeline", params + image_uri)
    Argo-->>Odoo: workflow_name
    Pipeline->>Pipeline: stage1 → eval1 → stage2 → eval2 → stage3 → eval3
    Pipeline->>Pipeline: finalize (aggregate metrics, push artifacts)
    Pipeline->>Odoo: POST /kaiju/callback/run {pass_rate, tests, cost, tokens, ...}
    Odoo->>Odoo: run_status = 'done', store metrics
```

### Workflow logs lifecycle

```mermaid
stateDiagram-v2
    [*] --> Pending: Workflow submitted
    Pending --> Running: First poll detects Argo phase=Running
    Running --> Running: Every 60s — _sync_steps()<br/>upserts step rows (phase/timestamps)
    Running --> Succeeded: Argo phase=Succeeded<br/>cron detects transition
    Running --> Failed: Argo phase=Failed/Error<br/>cron detects transition
    Succeeded --> [*]: _persist_step_logs() — fetch all pod logs once,<br/>store in step.log_text (survives pod GC)
    Failed --> [*]: Same — persist before pod cleanup
    note right of Running: User can manually click "Refresh Logs from Argo"<br/>on any step form to re-fetch live
```

---

## 10. Security

- All models granted full CRUD to `base.group_user` via `ir.model.access.csv`.
- Webhook routes are `auth="none"` but gated by bearer token check
  (`kaiju.webhook_token` config param).
- Argo API authenticated with the in-cluster ServiceAccount token mounted at
  `/var/run/secrets/kubernetes.io/serviceaccount/token` (Odoo must run as a
  pod with appropriate `WorkflowTemplate` permissions).

---

## 11. External Dependencies

| External System | Where Used | Contract |
|---|---|---|
| **Argo Workflows Server** | `argo_client.py` | REST + SSE; submit, poll, stop, logs |
| **EKS cluster** | Hosting Odoo + Argo | Provides ServiceAccount token for in-cluster auth |
| **S3 (e.g. `production-grtlabs-tag`)** | Read indirectly via pipeline outputs | Stored as `s3_dataset_uri` field |
| **ECR** | Read indirectly via pipeline outputs | Stored as `image_uri` field |

---

## 12. Recent Additions (Workflow Logs Feature)

| File | Type | Purpose |
|---|---|---|
| `models/kaiju_workflow_step.py` | NEW | Step model with `action_fetch_logs()` |
| `views/kaiju_workflow_step_views.xml` | NEW | Step list + form (terminal widget) |
| `models/argo_client.py` | MOD | + `_request_stream`, `list_workflow_nodes`, `get_pod_logs` (SSE parser) |
| `models/kaiju_commit0.py` | MOD | + `step_ids`, `_sync_steps`, `_persist_step_logs`, `_parse_argo_dt`; cron now syncs steps + persists logs on transition |
| `models/kaiju_commit0_run.py` | MOD | Same pattern for runs |
| `views/kaiju_commit0_views.xml` | MOD | + "Workflow Steps" inline list on build form |
| `views/kaiju_commit0_run_views.xml` | MOD | + "Workflow Steps" inline list on run form |
| `security/ir.model.access.csv` | MOD | + ACL row for `kaiju.commit0.workflow.step` |
| `__manifest__.py` | MOD | + step view XML registered |
