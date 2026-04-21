# Jaeger Phase 1 — Engineering Plan

> **Version:** 14.0.0  
> **Date:** 2026-04-20  
> **Status:** Implemented  
> **Scope:** Phase 1 only — Stage 1 (repo validation) + Stage 2 (SWE PR scraping pipeline)  
> **Module:** `ethara-etp/custom_addons/jaeger/`

---

## 1. Project Context

Jaeger produces software engineering task datasets for Meta's AI coding model training (RFP contract, POC: Kate Shapovalenko). The full pipeline has 7 stages. Phase 1 covers Stage 1–2. Stages 3–7 exist in the codebase but are disabled.

| Stage | Name | Status |
|-------|------|--------|
| **Stage 1** | Repo Validation | ✅ Active |
| **Stage 2** | PR Collection & Raw Dataset | ✅ Active |
| Stage 3 | Docker Build | Disabled |
| Stage 4 | Test Execution | Disabled |
| Stage 5 | Dataset Finalization | Disabled |
| Stage 6 | Trajectory Generation | Disabled |
| Stage 7 | Meta Delivery Export | Disabled |

Stage progression: `stage1 → stage2 → done`. Stages 3–7 action methods raise `UserError`, cron jobs are inactive, UI tabs/buttons are hidden.

Source repo: `multi-swe-bench` (ByteDance Seed) — pipeline tools vendored into `tools/`.

---

## 2. What Phase 1 Does

### 5-Step SWE Pipeline

```
Step 1: get_all_prs()       → {org}__{repo}_prs.jsonl
Step 2: filter_prs()        → {org}__{repo}_filtered_prs.jsonl
Step 3: get_related_issues() → {org}__{repo}_related_issues.jsonl
Step 4: merge_prs_with_issues() → {org}__{repo}_filtered_prs_with_issues.jsonl
Step 5: build_dataset()     → {org}__{repo}_raw_dataset.jsonl
```

**Step 1** — PyGithub `get_pulls("all")`, paginated at 100/page. Single token for the full pagination run. Writes all PR metadata to JSONL.

**Step 2** — Filters to `state == "closed"`. Fetches commit messages (1 API call/PR, token rotated every 50 PRs). Extracts resolved issues via regex (`(\w+)\s+\#(\d+)` against 9 keywords: close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved). Keeps only PRs with ≥1 resolved issue. Slowest step.

**Step 3** — Deduplicates issue numbers, fetches each via `get_issue()` (token rotated every 50 issues). Writes 4 fields: number, state, title, body.

**Step 5** — GitHub Compare API to get diffs (fresh token per PR, rate limit reported from HTTP response headers). Parses with `unidiff.PatchSet`. Splits into `fix_patch` (non-test files) and `test_patch` (files containing test/tests/e2e/testing). Skips PRs where either patch is empty. Retry: 3 attempts with fresh token on each retry, configurable delay. Permanent errors (404, 422) skipped. Appends to output file for crash recovery.

**After Step 5** — `_create_instances_from_dataset()` parses JSONL and creates `jaeger.instance` + `jaeger.resolved.issue` records. Commits every 100 instances. Enforces size limits: patches >5MB → skip PR, body >100KB → truncate.

### File Naming

`{org}__{repo}_{suffix}.jsonl` — double underscore separator.

### Typical Yield (repo with 5,000 PRs)

| Step | Output |
|------|--------|
| Step 1 | ~5,000 PRs |
| Step 2 | ~300–700 filtered |
| Step 3 | ~150–400 unique issues |
| Step 5 | ~200–350 valid instances |

---

## 3. System Architecture

Two dispatch modes controlled by `jaeger.dispatch_mode` setting:

### Production: Direct K8s Job Dispatch

```
┌─────────────────────────┐
│    ODOO SERVER           │
│                          │
│  Button click            │
│  → Validates inputs      │────────────┐
│  → Creates K8s Job       │            │
│  → Returns instantly     │            ▼
│                          │   ┌─────────────────────┐
│  Cron reconciles K8s     │   │  K8s JOB POD         │
│  Job status (2 min)      │   │                      │
│                          │   │  Bootstraps Odoo     │
│  Cron watchdog stale     │   │  Runs 5-step pipeline│
│  jobs (5 min)            │   │  Per-step cursors    │
└──────────┬───────────────┘   │  S3 upload per step  │
  ┌────────┴────────┐         │  Pod exits on done    │
  │  POSTGRESQL      │◄────────│                      │
  └─────────────────┘         └─────────────────────┘
  ┌────────┐                          │
  │ AWS S3 │◄─────────────────────────┘
  └────────┘
```

Odoo creates the K8s Job directly (`_create_scrape_k8s_job()`). Kueue manages scheduling. No message broker needed.

### Development: Local Background Thread

```
Button click → _run_pipeline_async() → spawns daemon thread
  → _run_scrape_pipeline_standalone(db_name, repo_id)
    → per-step cursors via _write_with_retry()
    → files written to local /tmp/jaeger_data/{org}__{repo}/
```

The standalone function uses per-step cursors — each DB write opens a connection for ~100ms, then releases it. No long-held connections. Safe for 50+ concurrent users.

### Pre-Dispatch Validation

`action_collect_prs()` validates before dispatch:
1. Stage must be `stage2`
2. GitHub tokens must be configured
3. Global concurrency cap: `MAX_CONCURRENT_SCRAPES = 500`
4. `SELECT ... FOR UPDATE` on `pr_collection_status` to prevent double-dispatch

### Status Flow

```
pending → queued → running → done
                      │
                      └──→ failed → pending (retry via button)
```

---

## 4. Module Structure

```
jaeger/
├── __manifest__.py              depends: [base, mail, web]
├── models/
│   ├── jaeger_repository.py     Main model + standalone pipeline functions
│   ├── jaeger_instance.py       One record = one PR/coding puzzle
│   ├── jaeger_resolved_issue.py Linked issue details
│   ├── jaeger_trajectory_run.py (Phase 2-7, inactive)
│   └── res_config_settings.py   Settings: tokens, S3, K8s, dispatch mode
├── tools/                       Vendored from multi-swe-bench/collect/
│   ├── get_all_prs.py           Step 1
│   ├── filter_prs.py            Step 2
│   ├── get_related_issues.py    Step 3
│   ├── merge_prs_with_issues.py Step 4
│   ├── build_dataset.py         Step 5
│   ├── github_token_pool.py     Round-robin token rotation
│   └── util.py                  extract_resolved_issues, datetime_serializer
├── worker/
│   ├── run_pipeline.py          K8s Job pod entrypoint
│   └── s3_helpers.py            boto3 upload/download/delete
├── controllers/
│   └── jaeger_controller.py     JSONL download endpoint + trajectory webhook
├── wizard/
│   └── import_repos_wizard.py   Bulk CSV import
├── views/
│   ├── jaeger_repository_views.xml
│   ├── jaeger_instance_views.xml
│   ├── jaeger_run_views.xml     (Phase 2-7, inactive)
│   ├── res_config_settings_views.xml
│   ├── import_repos_wizard_views.xml
│   └── jaeger_menus.xml
├── security/
│   ├── jaeger_security.xml      Groups, record rules
│   └── ir.model.access.csv      ACL
├── data/
│   ├── jaeger_data.xml          Sequence (JAE-0001)
│   └── cron.xml                 Watchdog + reconciliation (active), batch/EKS/auto-advance (disabled)
├── static/src/components/
│   ├── auto_refresh/            Polls form every 5s (active) / 8s (idle)
│   ├── instance_progress/       2-stage visual progress widget
│   └── run_dashboard/           (Phase 2-7, inactive)
└── tests/
```

---

## 5. Data Model

### `jaeger.repository`

| Field | Type | Purpose |
|-------|------|---------|
| `repo_url` | Char (required) | GitHub URL |
| `org` | Char (computed) | Extracted from URL |
| `repo_name` | Char (computed) | Extracted from URL |
| `language` | Selection | python/java/typescript/javascript/go/rust/c/cpp |
| `pipeline_mode` | Selection | swe/lht (only swe active) |
| `current_stage` | Selection | stage1 / stage2 / done |
| `pr_collection_status` | Selection | pending/queued/running/done/failed |
| `pr_collection_progress` | Float | 0–100% |
| `pr_collection_step` | Char | Live step description |
| `total_prs_fetched` | Integer | Step 1 count |
| `filtered_prs_count` | Integer | Step 2 count |
| `issues_fetched_count` | Integer | Step 3 count |
| `raw_dataset_count` | Integer | Step 5 count |
| `raw_dataset_jsonl_path` | Char | Path to final output |
| `log_output` | Text | Live log (truncated to 400 lines when >500) |
| `error_message` | Text | Failure details |

URL validation via `@api.constrains("repo_url")`: must match `https://github.com/{org}/{repo}`.

### `jaeger.instance`

| Field | Type | Purpose |
|-------|------|---------|
| `name` | Char | `{org}__{repo}-{number}` |
| `repository_id` | Many2one | Parent repo |
| `pr_number` | Integer | PR number |
| `base_sha` | Char | Base commit SHA |
| `fix_patch` | Text | Code fix diff |
| `test_patch` | Text | Test diff |
| `resolved_issues_json` | Text | JSON array of issue objects |
| `resolved_issue_ids` | One2many | Linked issue records |

### `jaeger.resolved.issue`

| Field | Type | Purpose |
|-------|------|---------|
| `instance_id` | Many2one | Parent instance |
| `issue_number` | Integer | GitHub issue # |
| `issue_title` | Char | Title |
| `issue_body` | Text | Body |

---

## 6. Pipeline Execution

### K8s Path (`worker/run_pipeline.py`)

Bootstraps Odoo inside the pod, reads config in a short cursor, then runs 5 steps. Between each step:

1. `_write_with_retry()` opens a cursor → writes progress → closes cursor
2. Tool function runs (no DB connection held)
3. `s3_helpers.upload()` saves output to S3
4. `_write_with_retry()` writes result counts

After Step 5: instance creation in a dedicated cursor. Then cleanup: delete intermediate S3 files, keep only `raw_dataset.jsonl`.

### Local Path (`_run_scrape_pipeline_standalone`)

Same pattern as K8s worker but writes to local `/tmp/jaeger_data/{org}__{repo}/` instead of S3. Uses `_write_with_retry()` and `_append_log_standalone()` for per-step DB writes.

### Progress Allocation

| Step | Progress | Duration |
|------|----------|----------|
| Step 1: Fetch PRs | 0% → 20% | 1–5 min |
| Step 2: Filter PRs | 25% → 40% | 5–75 min |
| Step 3: Fetch Issues | 45% → 60% | 2–10 min |
| Step 4: Merge | 65% → 80% | <10 sec |
| Step 5: Build Dataset | 82% → 95% | 5–30 min |
| Create Instances | 97% → 100% | 1–5 min |

### Vendored Tool Signatures

```python
get_all_prs.main(pool, out_dir, org, repo) → Path
filter_prs.main(pool, out_dir, prs_file, mode="swe", skip_commit_message=False) → Path
get_related_issues.main(pool, out_dir, filtered_prs_file) → Path
merge_prs_with_issues.main(out_dir, org, repo) → Path
build_dataset.main(pool, out_dir, merged_file, delay_on_error, retry_attempts) → Path
```

All API-calling tools accept a `GitHubTokenPool` instance as the first argument. Step 4 (`merge_prs_with_issues`) makes zero API calls and takes no pool.

---

## 7. Real-Time UI

### Auto-Refresh OWL Widget

`auto_refresh.js` polls the server via `record.load()` at 3s intervals when status is active (running/queued/building). Stops polling when idle. Matches the kaiju_build polling pattern.

### Instance Progress Widget

`instance_progress.js` renders a 2-stage visual indicator (Validation → PR Collection) with SVG progress rings.

---

## 8. GitHub Token Pool

`tools/github_token_pool.py` — thread-safe round-robin rotation with per-token rate limit tracking. Used by all pipeline steps and Stage 1 validation.

### Core API

- `get_token()`: returns the next available token, skipping any with <100 remaining calls. If all exhausted, sleeps until earliest reset + 5s.
- `report_usage(token, remaining, reset_at)`: updates internal tracking from GitHub API rate limit headers.
- `get_github_client(per_page=100)`: returns a `(Github, token)` tuple using a rotated token. Convenience wrapper for PyGithub steps.
- `report_from_client(g, token)`: reads `g.get_rate_limit().core` and feeds remaining/reset back to the pool.

### How Each Step Uses the Pool

| Step | Rotation Strategy | Feedback Mechanism |
|------|-------------------|--------------------|
| Step 1 (get_all_prs) | One client for the full pagination run | `report_from_client` after completion |
| Step 2 (filter_prs) | New client every 50 PRs | `report_from_client` at each rotation + end |
| Step 3 (get_related_issues) | New client every 50 issues | `report_from_client` at each rotation + end |
| Step 4 (merge_prs_with_issues) | No pool (zero API calls) | — |
| Step 5 (build_dataset) | Fresh `get_token()` per PR | `report_usage` from HTTP `X-RateLimit-*` headers per response |

### Pool Lifecycle

A fresh `GitHubTokenPool` is created at the start of each pipeline run (both K8s and local paths). Tokens are read from `ir.config_parameter` via a short DB cursor, then the pool is constructed and passed to all 4 API-calling steps. The pool is not shared across pipeline runs.

Stage 1 validation uses a separate process-level singleton pool via `get_token_pool(env)`.

### Configuration

Comma-separated PATs in `ir.config_parameter` key `jaeger.github_tokens`. Configured via Settings → Jaeger → GitHub Tokens.

---

## 9. S3 File Storage

### K8s Path

`worker/s3_helpers.py` wraps `boto3`:
- `upload(local_path, repo_id, filename)` → S3 key
- `download(repo_id, filename, local_path)`
- `delete(repo_id, filename)`
- `delete_prefix(repo_id)` — bulk cleanup

S3 key format: `{prefix}/{repo_id}/{filename}` (default prefix: `jaeger/phase1`).

Intermediate files deleted after Step 5. Only `raw_dataset.jsonl` kept permanently.

Config via env vars: `JAEGER_S3_BUCKET`, `JAEGER_S3_REGION`, `JAEGER_S3_PREFIX`.

### Local Path

Files written to `/tmp/jaeger_data/{org}__{repo}/`. Path stored in `raw_dataset_jsonl_path` field. Downloadable via the "Download Raw Dataset JSONL" button.

---

## 10. Error Handling

### Pipeline-Level

`_run_scrape_pipeline_standalone` wraps the 5 steps in try/except. On failure: writes `pr_collection_status = "failed"`, `error_message`, appends to log, pushes `jaeger/pipeline_failed` notification.

### Step 5 Per-PR Retry

Built into `build_dataset.py`: 3 attempts per PR with configurable delay (default 300s). Each retry gets a fresh token from the pool. Permanent errors (404, 422, "No common ancestor") skip the PR immediately.

### Serialization Retry

`_write_with_retry()` retries 3 times with `time.sleep(1 + attempt)` on PostgreSQL serialization conflicts.

### Cron Watchdog (active, every 5 min)

`_cron_watchdog_stale_scrapes()`: repos stuck in `running` for >60 min with no `write_date` update → marked `failed`.

### K8s Reconciliation Cron (active, every 2 min)

`_cron_reconcile_scrape_jobs()`: checks K8s Job status for `running` repos. If Job succeeded but DB not updated (OOM kill, node failure) → marks `done`. If Job failed → marks `failed` with pod logs.

### Log Truncation

`_append_log` / `_append_log_standalone`: when log exceeds 500 lines, truncates to last 400.

---

## 11. Security

### Groups (Odoo 19 `res.groups.privilege`)

| Group | Repository | Instance | Resolved Issue |
|-------|-----------|----------|----------------|
| User | read/write/create | read/write | read |
| Admin | full CRUD | full CRUD | full CRUD |

### Record Rules

- Users see own repos only (`user_id = user.id`)
- Admins see all repos

### Token Security

GitHub tokens stored in `ir.config_parameter` (server-side only). Read via `sudo().get_param()`. Never exposed to browser. Admin-only Settings page.

### SUPERUSER_ID in Workers

Background threads and K8s pods use `SUPERUSER_ID` for ORM access. Security boundary is the button method — it validates user access via Odoo record rules before dispatching.

---

## 12. Infrastructure

### DevOps Checklist (for K8s dispatch mode)

- [ ] S3 bucket created with lifecycle rule (30-day auto-delete for `jaeger/phase1/*/`)
- [ ] IAM role with `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket` scoped to bucket
- [ ] K8s namespace `jaeger` with service account `jaeger-pipeline-runner`
- [ ] RBAC: service account can create/list/delete Jobs in `jaeger` namespace
- [ ] Kueue LocalQueue `jaeger-scraping` pointing to `general-purpose` ClusterQueue
- [ ] `PyGithub` and `unidiff` added to Docker image (`pip install PyGithub unidiff`)
- [ ] `K8s Job image` configured in Settings → Jaeger → K8s Dispatch
- [ ] GitHub tokens configured in Settings → Jaeger → GitHub Tokens
- [ ] Node selector labels: `kubernetes.io/arch: amd64`, `ethara.ai/node-pool: general-purpose`

### K8s Job Spec (created by `_create_scrape_k8s_job`)

- Image: from `jaeger.k8s_job_image` setting
- Command: `python custom_addons/jaeger/worker/run_pipeline.py`
- Env: `REPO_ID`, `ODOO_DB`, `JAEGER_S3_BUCKET`, `JAEGER_S3_REGION`, `JAEGER_S3_PREFIX`
- Resources: 500m CPU / 2Gi memory / 5–10Gi ephemeral storage
- `backoffLimit: 2`, `ttlSecondsAfterFinished: 3600`, `activeDeadlineSeconds: 7200`
- Kueue label: `kueue.x-k8s.io/queue-name: jaeger-scraping`

### For Local Dispatch (no infra needed)

Set `jaeger.dispatch_mode = local` in Settings. Pipeline runs in a background thread using local `/tmp` for files. No S3, no K8s.

---

## 13. Download & Preview

### Download Endpoint

`GET /jaeger/download/<repo_id>/raw_dataset` — serves the JSONL file with `Content-Disposition` header. Requires authenticated user (`auth="user"`).

Button: "Download Raw Dataset JSONL" visible in Stage 2 tab when `pr_collection_status == done`.

### Preview

`raw_dataset_preview` computed field shows the first few lines of the JSONL in the form view.

---

## 14. Settings Reference

| Key | Default | Purpose |
|-----|---------|---------|
| `jaeger.github_tokens` | (required) | Comma-separated GitHub PATs |
| `jaeger.output_dir` | `/tmp/jaeger_data` | Local file output directory |
| `jaeger.retry_attempts` | `3` | Per-PR retry in Step 5 |
| `jaeger.delay_on_error` | `300` | Seconds to wait on rate limit |
| `jaeger.dispatch_mode` | `local` | `local` or `k8s` |
| `jaeger.k8s_job_image` | (required for k8s) | Docker image for pipeline pods |
| `jaeger.s3_bucket` | (required for k8s) | S3 bucket name |
| `jaeger.s3_region` | `ap-south-1` | AWS region |
| `jaeger.s3_prefix` | `jaeger/phase1` | S3 key prefix |
