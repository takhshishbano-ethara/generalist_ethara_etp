# Skoll Module — AGENTS.md

> LLM task management with sandbox environments. Odoo 19 custom addon.

## Identity

- **Technical Name**: `skoll`
- **Version**: 19.0.7.0.0
- **Category**: Tools
- **License**: LGPL-3
- **Dependencies**: `base`, `web`, `hr`, `bus`, `etp_user_roles`
- **Application**: Yes (top-level menu)

## What Skoll Does

Skoll orchestrates LLM task execution pipelines. A "task" (`skoll.skoll`) defines a prompt + persona. For each task, multiple sandboxed environments spin up (one per model variant: claude, glm, 1pa–1pd), each running an OpenClaw agent + LiteLLM proxy + PostgreSQL. The system captures agent trajectories, runs automated QC, generates golden trajectories, and exports delivery-ready data.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Odoo (skoll module)                                             │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ Models   │  │ Controllers  │  │ Frontend (OWL)           │  │
│  │ skoll    │  │ chat         │  │ task_dashboard            │  │
│  │ sandbox  │  │ qc           │  │ chat_widget              │  │
│  │ k8s      │  │ auto_hint    │  │ sandbox_card/iframe      │  │
│  │ persona  │  │ export       │  │ costing_dashboard        │  │
│  └──────────┘  └──────────────┘  └──────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│ Background Workers (ThreadPoolExecutor)                         │
│  golden_generation(2) | taskdesc(2) | sandbox_start(3) | hint(3)│
├─────────────────────────────────────────────────────────────────┤
│ RabbitMQ Pipeline (consumer.py — runs OUTSIDE Odoo)             │
│  claim_task → start_sandbox → ws_connect → prompt → save → hint │
└─────────────────────────────────────────────────────────────────┘
         │                                        │
         ▼                                        ▼
┌─────────────────┐              ┌────────────────────────────┐
│ K8s / Docker    │              │ AWS Services               │
│ Sandbox Pod:    │              │ - Bedrock (LLM inference)  │
│  - OpenClaw     │              │ - S3 (session backup)      │
│  - LiteLLM     │              │ - ECR (container images)   │
│  - PostgreSQL   │              │                            │
│  - session-bkp  │              │                            │
└─────────────────┘              └────────────────────────────┘
```

## Deployment Modes

| Mode | Toggle | Infrastructure |
|------|--------|---------------|
| `local` | `skoll.deployment_mode = local` | Docker Compose via `sandbox_docker/` |
| `k8s` | `skoll.deployment_mode = k8s` | Kubernetes (namespace: `skoll`, node pool: `general-purpose`) |

## Key Configuration Parameters

All stored as `ir.config_parameter` keys:

| Key | Purpose |
|-----|---------|
| `skoll.deployment_mode` | `local` or `k8s` |
| `skoll.openclaw_image` | OpenClaw container image |
| `skoll.litellm_image` | LiteLLM proxy image |
| `skoll.ws_router_host` | WebSocket router hostname |
| `skoll.bedrock_inference_arn` | AWS Bedrock model ARN |
| `skoll.bedrock_region` | AWS region for Bedrock |
| `skoll.s3_bucket` / `skoll.s3_region` / `skoll.s3_prefix` | S3 session backup |
| `skoll.k8s_namespace` | K8s namespace (default: `skoll`) |
| `skoll.disable_prompt_qc` | Skip prompt QC step |
| `skoll.disable_trajectory_qc` | Skip trajectory QC step |
| `skoll.disable_auto_hint` | Disable auto-hint loop |

## Directory Layout

```
skoll/
├── models/              → AGENTS.md (6 models, core logic)
├── controllers/         → AGENTS.md (8 controllers, HTTP API)
├── static/src/          → AGENTS.md (OWL frontend)
├── tests/               → AGENTS.md (24 test files)
├── services/            # RabbitMQ publish helpers (single file)
├── views/               # Standard Odoo XML views
├── security/            # ACL + security groups
├── data/                # Cron + seed data
├── migrations/          # DB migrations (19.0.6.0.0, 19.0.7.0.0)
├── prompts/             # Auto-hint prompt templates (.md)
├── sandbox_docker/      # Docker Compose for local mode
├── consumer.py          # Standalone RabbitMQ worker
├── ws_client.py         # WebSocket client for sandbox comms
├── Delivery_Schema.json # Trajectory delivery JSON schema
├── SKOLL_K8S_WORKFLOW.md # K8s architecture docs (757 lines)
└── *.md                 # LLM prompt templates (golden, QC, taskdesc)
```

## Conventions

### Python

- 4-space indent, Odoo ORM patterns throughout
- Model naming: `skoll.{entity}` (dot-separated)
- Imports: `from odoo import models, fields, api, _` at top
- Background work: `ThreadPoolExecutor` at module level (NOT `ir.cron`)
- External API calls: `httpx` (async-capable), NOT `requests`
- Config access: `self.env['ir.config_parameter'].sudo().get_param('skoll.*')`
- Logging: `_logger = logging.getLogger(__name__)`

### Frontend (OWL)

- Component triplets: `component_name.js` + `component_name.xml` + `component_name.scss`
- Services: standalone `.js` files in `static/src/`
- Registry: `registry.category("actions").add("skoll.action_name", Component)`
- No React, no Vue — pure Odoo OWL framework

### Security

- Groups defined in `security/skoll_security.xml`
- ACL in `security/ir.model.access.csv`
- Three roles: Tasker (own records), Quality Lead (all records), Admin (full CRUD)

### Prompts

- Root-level `.md` files: golden trajectory, trajectory QC, golden QC, task description, system prompts
- `prompts/` directory: auto-hint generation, satisfaction eval, LLM assist, LLM QC
- Prompt files are read at runtime via `open()` — they are NOT stored in DB

### Versioning & Migrations

- Version format: `19.0.MAJOR.MINOR.PATCH`
- Migrations in `migrations/{version}/pre-migrate.py`
- Bump version in `__manifest__.py` when adding/changing fields

## Anti-Patterns (DO NOT)

- **NEVER** use `as any` / `@ts-ignore` equivalents — fix the type
- **NEVER** fabricate data in golden trajectories
- **NEVER** use `seconds=60` in time values (Odoo cron quirk)
- **NEVER** use `requests` library — use `httpx` for HTTP calls
- **NEVER** put business logic in controllers — controllers are thin HTTP adapters
- **NEVER** suppress Odoo ORM warnings with `sudo()` unless security-justified
- **NEVER** hardcode AWS credentials — use IAM roles / config parameters

## Running Tests

```bash
# All skoll tests (requires running Odoo + PostgreSQL)
python src/odoo-bin --test-enable --test-tags=skoll --stop-after-init -u skoll -d <db_name>

# Specific test class
python src/odoo-bin --test-enable --test-tags=skoll -u skoll -d <db_name> 2>&1 | grep "test_"
```

## Related Modules

- **kensei** — Sibling module (LLM tasks + file attachments), shares ~80% frontend code
- **atlas** — GLM 5 variant, near-identical sandbox architecture
- **etp_user_roles** — Provides security groups Skoll depends on
- **ai_services** — Shared Bedrock/document-parsing service layer
