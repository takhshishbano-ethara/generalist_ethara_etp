# Jaeger Pipeline — Engineering Plan

> **Version:** 18.0.0
> **Date:** 2026-04-22
> **Status:** Phase 1 implemented, Phase 2 (Stages 3-5) enabled + Stage 3 hardened + Stage 4 production-grade + Human-in-the-Loop Test Config + Parser Fixes (AVA, Mocha)
> **Scope:** Stage 1 (repo validation) + Stage 2 (PR collection) + Stage 3 (Docker build) + Stage 4 (test execution) + Stage 5 (dataset finalization) + Test Config overrides
> **Module:** `ethara-etp/custom_addons/jaeger/`

---

## 1. Project Context

Jaeger produces software engineering task datasets for Meta's AI coding model training (RFP contract, POC: Kate Shapovalenko). The full pipeline has 7 stages across 3 phases.

| Stage | Name | Phase | Status |
|-------|------|-------|--------|
| **Stage 1** | Repo Validation | Phase 1 | Active |
| **Stage 2** | PR Collection & Raw Dataset | Phase 1 | Active |
| **Stage 3** | Docker Build | Phase 2 | Active |
| **Stage 4** | Test Execution (3-Run) | Phase 2 | Active |
| **Stage 5** | Dataset Finalization | Phase 2 | Active |
| Stage 6 | Trajectory Generation | Phase 3 | Disabled |
| Stage 7 | Meta Delivery Export | Phase 3 | Disabled |

Stage progression: `stage1 → stage2 → stage3 → stage4 → stage5 → stage6 → stage7 → done`. Stages 6-7 action methods raise `UserError`, UI buttons are hidden.

Source repo: `multi-swe-bench` (ByteDance Seed) — pipeline tools vendored into `tools/`.

### RabbitMQ Status

`consumer.py` was deleted in commit `80d01aac8`. `services/rabbitmq_service.py` still exists (274 lines) but publishes to queues nobody consumes. All "Queue" action buttons remain hidden. Only "Direct" variants (local background threads) are functional.

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

## 3. What Phase 2 Does

Phase 2 covers Stages 3-5: Docker image building, test execution, and dataset finalization. It takes the raw dataset from Phase 1 and produces production-grade f2p/p2p/s2p/n2p test classifications for every instance.

### Stage 3: Docker Image Building

Three-layer auto-build chain:

```
Layer 0: Language Runtime (external, always available)
  LANGUAGE_BASE_IMAGES[repo.language]
  python:3.11-slim / node:20-slim / golang:1.22 / rust:1.77 / etc.

Layer 1: Repo Base Image (built ONCE per repo, cached)
  mswebench/{org}_m_{repo}:base
  - Shallow-clone repo + detect & install deps
  - Install test framework
  - Set WORKDIR /testbed

Layer 2: PR Instance Image (built per-PR)
  mswebench/{org}_m_{repo}:pr-{N}
  FROM mswebench/{org}_m_{repo}:base
  - git fetch origin && git checkout {base_sha}
  - COPY fix-run.sh /jaeger/fix-run.sh
  - For JS/TS: also runs dep reinstall if package.json changed
```

**Fallback:** If `swebench/sweb.eval.x86_64.{org}_1776_{repo}-{N}:latest` exists locally, use it directly (skip Layers 0+1).

Key methods in `jaeger_repository.py`:
- `_build_base_image()` (line 1674) — Layer 1 build with language detection
- `_generate_dockerfile()` (line 1930) — Layer 2 per-PR image with SWE-bench fallback
- `_build_via_local_docker()` (line 1822) — orchestrates builds for all instances
- `_detect_install_commands()` (line 1612) — dependency detection per language
- `run_docker_build()` (line 1531) — entry point called by background thread

**Language Base Images** (line 76):

| Language | Base Image |
|----------|-----------|
| python | `python:3.11-slim` |
| javascript | `node:20-slim` |
| typescript | `node:20-slim` |
| java | `eclipse-temurin:17-jdk` |
| go | `golang:1.22` |
| rust | `rust:1.85` |
| c | `ubuntu:22.04` |
| cpp | `ubuntu:22.04` |

Fields: `base_image_name` (Char), `base_image_status` (Selection: none/building/built/failed), `docker_platform` (Char, empty = native).

### Stage 4: Test Execution (3-Run Pattern)

Each instance runs 3 Docker executions:
1. **Baseline** (no patches) → `run.log`
2. **Test-patch only** → `test-patch-run.log`
3. **Fix + test patch** → `fix-patch-run.log`

**Two execution paths:**

1. **Parallel (preferred):** `_run_all_tests()` → `ThreadPoolExecutor(max_workers=2)` → `_run_instance_tests_standalone()` per instance. Pure function with own DB cursors. No ORM held during Docker runs.
2. **ORM-based (legacy):** `run_test_execution()` (jaeger_instance.py) — sequential per-instance, used for single-instance re-runs.

Both paths use the same Docker execution and parsing logic.

**Docker execution:** `_execute_docker_run_pure()` (standalone) / `_execute_docker_run()` (ORM). Volume-mounted patches applied at runtime, same image reused for all 3 runs.

**Network policy:** `--network none` for Python only. Non-Python languages (Rust, JS, Go, Java, etc.) need network access for runtime dependency resolution (`cargo test` fetches deps, `npm test` may download, etc.). This was verified empirically: `cargo test --no-run` with `--network none` fails with "Could not resolve host: index.crates.io".

**Memory limits:** Language-aware: 8GB for compilation-heavy languages (Rust, C, C++, Java), 4GB for others.

**Early skip optimization:** After Run 2 (test-patch), if 0 test failures detected, Run 3 is skipped — f2p is mathematically impossible if nothing fails with only the test patch.

#### Language-Aware Test Commands

`_generate_fix_run_script()` (jaeger_repository.py) generates per-language test commands:

| Language | Command | Notes |
|----------|---------|-------|
| python | `python -m pytest {test_files or 'tests/'} -v` | Default for unknown languages |
| javascript | Runtime-adaptive: `_generate_js_fix_run_script()` | Inspects package.json at checked-out commit |
| typescript | Runtime-adaptive: `_generate_js_fix_run_script()` | Same as JavaScript |
| go | `go test -v -count=1 -timeout 15m {packages}` | Extracts packages from test file paths |
| rust | `cargo test` | |
| java | `mvn clean test -fn` or `./gradlew test` | Auto-detects pom.xml vs build.gradle |
| c | `cmake .. && make && ctest` or `make test` | Auto-detects CMakeLists.txt vs Makefile |
| cpp | `cmake -DBUILD_TESTING=ON .. && make && ctest --output-on-failure` | |

**JS/TS Runtime-Adaptive Script:** Instead of hardcoding `npm test` from HEAD, `_generate_js_fix_run_script()` generates a bash script that:
1. Runs `npm install --ignore-scripts` at the checked-out base_sha
2. Reads `package.json` at that commit to detect `scripts.test`
3. Falls back to framework detection: jest → mocha → ava → vitest
This handles repos where older commits use different test frameworks (e.g., chalk switching from mocha to ava).

#### Multi-Framework Log Parser

`_parse_test_log()` (jaeger_instance.py:385) auto-detects the test framework from log content and delegates to 7 parsers:

| Priority | Parser | Detection Signal | Method |
|----------|--------|-----------------|--------|
| 1 | Go | `--- PASS:` / `--- FAIL:` | `_parse_go_log()` (line 710) |
| 2 | Rust | `test \S+ ... ok/FAILED/ignored` | `_parse_rust_log()` (line 740) |
| 3 | Mocha | `\d+ passing` OR `\d+ failing` | `_parse_mocha_log()` (line 779) |
| 4 | AVA | `\d+ tests? failed` / `\d+ (tests?)? passed` | `_parse_ava_log()` (line 631) |
| 5 | Jest/Vitest | `✓` / `✕` / `○` symbols | `_parse_jest_log()` (line 760) |
| 6 | CTest | `Test #N:` / `[ PASS ]` / `[ FAIL ]` | `_parse_ctest_log()` (line 820) |
| 7 | Maven/Surefire | `[INFO/ERROR].*Tests run:` | `_parse_maven_log()` (line 850+) |
| 8 | pytest | (default fallback) | `_parse_pytest_log()` (line 692) |

Each parser returns `{passed_count, failed_count, skipped_count, passed_tests, failed_tests, skipped_tests}`.

**Detection order is critical:** Mocha and Jest both use `✓` (U+2713). Mocha is detected first by checking for `\d+ passing` / `\d+ failing` summary lines (which jest doesn't emit). AVA is checked before Jest because AVA's spinner output after ANSI stripping can contain `✓`. Go parser has priority handling: FAIL wins over PASS for the same test name.

#### Test Classification

`_generate_test_report()` (jaeger_instance.py:608) compares the 3 runs and classifies each test:

| Classification | Meaning |
|---------------|---------|
| **f2p** (fail-to-pass) | Failed in baseline, passed after fix+test patch |
| **p2p** (pass-to-pass) | Passed in both baseline and fix+test |
| **s2p** (skip-to-pass) | Skipped in baseline, passed after fix+test |
| **n2p** (new-to-pass) | Not in baseline, appeared and passed after fix+test |

Instances with f2p > 0 and no regressions are marked valid.

### Stage 5: Dataset Finalization

`_build_final_dataset()` (jaeger_repository.py:2167) writes the final JSONL with valid instances. Only instances that pass all quality gates (f2p > 0, no regressions) are included.

`_run_all_tests()` (jaeger_repository.py:2100) iterates all built instances calling `run_test_execution()`.

---

## 4. System Architecture

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

### Sandbox Environment

`custom_addons/jaeger/sandbox/` provides a local dev environment:
- **docker-compose.yml**: PostgreSQL 16 + MinIO (S3-compatible) + Odoo + Nginx + K3s + bucket creator
- **setup.sh / teardown.sh**: One-command spin up/down
- **Dockerfile.sandbox + odoo.conf**: Full local Odoo container

Services:
- Odoo: `http://localhost:8069` (admin/admin)
- MinIO Console: `http://localhost:9001` (minioadmin/minioadmin)
- K3s API: `https://localhost:6443`
- S3 bucket: `jaeger-local` on MinIO

The sandbox is for running Odoo + Phase 1/2 locally. Docker image building (Phase 2 Stage 3) uses `subprocess.run(["docker", "build", ...])` on the host machine directly — it is NOT related to the sandbox.

---

## 5. Module Structure

```
jaeger/
├── __manifest__.py              depends: [base, mail, web]
├── models/
│   ├── jaeger_repository.py     Main model + all pipeline actions (~2750 lines)
│   ├── jaeger_instance.py       Instance model + 3-run test execution + log parsers
│   ├── jaeger_resolved_issue.py Linked issue details
│   ├── jaeger_trajectory_run.py (Phase 3, inactive)
│   └── res_config_settings.py   Settings: tokens, S3, K8s, dispatch mode
├── tools/                       Vendored from multi-swe-bench/collect/
│   ├── get_all_prs.py           Step 1
│   ├── filter_prs.py            Step 2
│   ├── get_related_issues.py    Step 3
│   ├── merge_prs_with_issues.py Step 4
│   ├── build_dataset.py         Step 5
│   ├── github_token_pool.py     Round-robin token rotation
│   ├── dataset_converter.py     Meta 26-field schema converter
│   └── util.py                  extract_resolved_issues, datetime_serializer
├── services/
│   └── rabbitmq_service.py      Dead code (consumer.py deleted)
├── worker/
│   ├── run_pipeline.py          K8s Job pod entrypoint
│   └── s3_helpers.py            boto3 upload/download/delete
├── controllers/
│   └── jaeger_controller.py     JSONL download endpoint + trajectory webhook
├── wizard/
│   └── import_repos_wizard.py   Bulk CSV import
├── sandbox/
│   ├── docker-compose.yml       PostgreSQL + MinIO + Odoo + Nginx + K3s
│   ├── setup.sh / teardown.sh   One-command environment management
│   ├── Dockerfile.sandbox       Local Odoo container
│   ├── odoo.conf                Sandbox Odoo configuration
│   ├── s3_patch.py              MinIO endpoint patching
│   └── nginx/default.conf       Reverse proxy config
├── views/
│   ├── jaeger_repository_views.xml  Stage-aware button visibility
│   ├── jaeger_instance_views.xml
│   ├── jaeger_run_views.xml     (Phase 3, inactive)
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
│   ├── instance_progress/       Visual progress widget
│   └── run_dashboard/           (Phase 3, inactive)
└── tests/
```

---

## 6. Data Model

### `jaeger.repository`

| Field | Type | Purpose |
|-------|------|---------|
| `repo_url` | Char (required) | GitHub URL |
| `org` | Char (computed) | Extracted from URL |
| `repo_name` | Char (computed) | Extracted from URL |
| `language` | Selection | python/java/typescript/javascript/go/rust/c/cpp |
| `pipeline_mode` | Selection | swe/lht |
| `current_stage` | Selection | stage1-stage7 / done / failed |
| `pr_collection_status` | Selection | pending/queued/running/done/failed |
| `pr_collection_progress` | Float | 0–100% |
| `pr_collection_step` | Char | Live step description |
| `total_prs_fetched` | Integer | Step 1 count |
| `filtered_prs_count` | Integer | Step 2 count |
| `issues_fetched_count` | Integer | Step 3 count |
| `raw_dataset_count` | Integer | Step 5 count |
| `raw_dataset_jsonl_path` | Char | Path to final output |
| `docker_build_status` | Selection | none/queued/building/built/failed |
| `base_image_name` | Char | Layer 1 Docker image tag |
| `base_image_status` | Selection | none/building/built/failed |
| `test_execution_status` | Selection | none/queued/running/done/failed |
| `dataset_status` | Selection | none/queued/generating/done/failed |
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
| `docker_build_status` | Selection | none/building/built/failed |
| `docker_image_tag` | Char | Full Docker image tag |
| `language` | Selection (related) | From parent repo |
| `selected_test_files_json` | Text | JSON array of test file paths |
| `run_log` | Text | Baseline test output |
| `test_patch_log` | Text | Test-patch-only output |
| `fix_patch_log` | Text | Fix+test output |
| `f2p_tests` | Text | JSON: fail-to-pass tests |
| `p2p_tests` | Text | JSON: pass-to-pass tests |
| `s2p_tests` | Text | JSON: skip-to-pass tests |
| `n2p_tests` | Text | JSON: new-to-pass tests |

### `jaeger.resolved.issue`

| Field | Type | Purpose |
|-------|------|---------|
| `instance_id` | Many2one | Parent instance |
| `issue_number` | Integer | GitHub issue # |
| `issue_title` | Char | Title |
| `issue_body` | Text | Body |

---

## 7. Pipeline Execution

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

## 8. Stage Progression

### `_next_stage()` (jaeger_repository.py:2717)

```python
mapping = {
    "stage1": "stage2",
    "stage2": "stage3",
    "stage3": "stage4",
    "stage4": "stage5",
    "stage5": "stage6",
    "stage6": "stage7",
    "stage7": "done",
}
```

### `action_advance_stage()` (jaeger_repository.py:2671)

Checks gate conditions per stage, then advances. Only blocked in terminal states (`done`, `failed`).

### SWE Standalone Path

After Phase 1 completes, `_run_swe_steps_standalone()` sets `current_stage = "stage3"` (line 574), allowing the repo to proceed into Phase 2 Docker builds.

### UI Buttons

Visible buttons per stage (in `jaeger_repository_views.xml`):
- **Stage 2:** "Collect PRs" / "Collect PRs (Local)"
- **Stage 3:** "Build Images" — visible when `current_stage == 'stage3'` and not already building
- **Stage 4:** "Run Tests" — visible when `current_stage == 'stage4'` and not already running
- **Stage 5:** "Finalize Dataset" — visible when `current_stage == 'stage5'` and not already generating

"Advance Stage" button visible for all non-terminal stages. Statusbar shows: `stage1, stage2, stage3, stage4, stage5, done`.

### Remaining Gates (Phase 3)

3 `raise UserError` gates remain for Stages 6-7:
- `action_dispatch_trajectories` (line 2273) — Stage 6
- `action_export_meta` (line 2530) — Stage 7
- `action_export_meta_direct` (line 2540) — Stage 7

---

## 9. Real-Time UI

### Auto-Refresh OWL Widget

`auto_refresh.js` polls the server via `record.load()` at 3s intervals when status is active (running/queued/building). Stops polling when idle. Matches the kaiju_build polling pattern.

### Instance Progress Widget

`instance_progress.js` renders a visual stage indicator with SVG progress rings.

---

## 10. GitHub Token Pool

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

## 11. S3 File Storage

### K8s Path

`worker/s3_helpers.py` wraps `boto3`:
- `upload(local_path, repo_id, filename)` → S3 key
- `download(repo_id, filename, local_path)`
- `delete(repo_id, filename)`
- `delete_prefix(repo_id)` — bulk cleanup

S3 key format: `{prefix}/{repo_id}/{filename}` (default prefix: `jaeger/phase1`).

Intermediate files deleted after Step 5. Only `raw_dataset.jsonl` kept permanently.

Config via env vars: `JAEGER_S3_BUCKET`, `JAEGER_S3_REGION`, `JAEGER_S3_PREFIX`. Supports `JAEGER_S3_ENDPOINT` for MinIO compatibility (sandbox).

### Local Path

Files written to `/tmp/jaeger_data/{org}__{repo}/`. Path stored in `raw_dataset_jsonl_path` field. Downloadable via the "Download Raw Dataset JSONL" button.

---

## 12. Error Handling

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

## 13. Security

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

## 14. Infrastructure

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
- [ ] Docker available on host (for Phase 2 local image builds via `subprocess`)

### K8s Job Spec (created by `_create_scrape_k8s_job`)

- Image: from `jaeger.k8s_job_image` setting
- Command: `python custom_addons/jaeger/worker/run_pipeline.py`
- Env: `REPO_ID`, `ODOO_DB`, `JAEGER_S3_BUCKET`, `JAEGER_S3_REGION`, `JAEGER_S3_PREFIX`
- Resources: 500m CPU / 2Gi memory / 5–10Gi ephemeral storage
- `backoffLimit: 2`, `ttlSecondsAfterFinished: 3600`, `activeDeadlineSeconds: 7200`
- Kueue label: `kueue.x-k8s.io/queue-name: jaeger-scraping`

### For Local Dispatch (no infra needed)

Set `jaeger.dispatch_mode = local` in Settings. Pipeline runs in a background thread using local `/tmp` for files. No S3, no K8s. Docker must be available on the host for Phase 2 image builds.

---

## 15. Download & Preview

### Download Endpoint

`GET /jaeger/download/<repo_id>/raw_dataset` — serves the JSONL file with `Content-Disposition` header. Requires authenticated user (`auth="user"`).

Button: "Download Raw Dataset JSONL" visible in Stage 2 tab when `pr_collection_status == done`.

### Preview

`raw_dataset_preview` computed field shows the first few lines of the JSONL in the form view.

---

## 16. Settings Reference

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

---

## 17. Changelog

### v18.0.0 (2026-04-22) — Human-in-the-Loop Test Config + Parser Fixes

Added a `test_config_json` field to `jaeger.repository` that lets a human override auto-detected pipeline settings. Auto-detection remains the default; human intervenes only when it fails. Also fixed AVA and Mocha parsers, reordered parser detection, and fixed post-build validation false positives.

#### Why This Was Needed

The multi-swe-bench reference has **2,813 hand-written Python config files** — one per repo — defining base image, install commands, test commands, system deps, and custom log parsers. Each was tuned by a human. Our pipeline auto-detects these, which works for ~60% of repos but fails for repos with unusual setups (yarn vs npm vs pnpm, custom system deps, PR-range-specific base images, custom test runner invocations like `npx ava` vs `npm test`).

**Core problem:** `npm test` in many JS repos runs `linter && test-runner`. Old commits often fail the linter (style changes over time), so tests never execute. Human override to run the test runner directly (e.g., `npx ava`) bypasses this.

#### New Fields on `jaeger.repository`

```python
# Line 720-729 in jaeger_repository.py
test_config_json = fields.Text(
    string="Test Configuration (JSON)",
    help="Optional JSON overrides for auto-detected settings. "
         "Keys: base_image, system_deps, install_cmd, test_cmd, "
         "prepare_cmd, parser, memory_limit, network, env",
)
test_config_effective = fields.Text(
    string="Effective Config",
    compute="_compute_test_config_effective",
)
```

#### JSON Schema

```json
{
  "base_image": "node:22",
  "system_deps": ["cmake", "g++", "pkg-config"],
  "install_cmd": "yarn install --frozen-lockfile",
  "test_cmd": "yarn test -- --verbose",
  "prepare_cmd": "yarn build",
  "parser": "mocha",
  "memory_limit": "8g",
  "network": true,
  "env": {"NODE_OPTIONS": "--max-old-space-size=4096"}
}
```

All keys are optional. Missing keys = auto-detected (current behavior). Single JSON field (not 10 separate Char fields) because config needs vary wildly per language.

#### Architecture: 5 Injection Points

| # | Method | File:Line | Override Keys | Fallback |
|---|--------|-----------|---------------|----------|
| 1 | `_build_base_image()` | jaeger_repository.py:1857 | `base_image`, `system_deps`, `install_cmd`, `env` | `LANGUAGE_BASE_IMAGES[]`, language-based apt-get, `_detect_install_commands()` |
| 2 | `_generate_dockerfile()` | jaeger_repository.py:2215 | `install_cmd` (for dep reinstall) | `_dep_reinstall_commands()` |
| 3 | `_generate_fix_run_script()` | jaeger_repository.py:2276 | `test_cmd`, `prepare_cmd` | Language switch + `_generate_js_fix_run_script()` |
| 4 | `_execute_docker_run_pure()` | jaeger_instance.py:10 | `memory_limit`, `network` | Language-based 4g/8g, Python-only `--network none` |
| 5 | `_run_instance_tests_standalone()` | jaeger_instance.py:117 | `memory_limit`, `network` (read from DB) | Same as #4 |

Plus the ORM path `_execute_docker_run()` (jaeger_instance.py:547) also reads config.

#### Core Method: `_get_effective_config()` (Line 1700)

Single source of truth — all injection points call this:

```python
def _get_effective_config(self):
    config = {}
    if self.test_config_json:
        try:
            config = json.loads(self.test_config_json)
        except (json.JSONDecodeError, TypeError):
            pass
    lang = (self.language or "python").lower()
    config.setdefault("base_image", LANGUAGE_BASE_IMAGES.get(lang, "python:3.11-slim"))
    config.setdefault("memory_limit", "8g" if lang in ("rust", "cpp", "c", "java") else "4g")
    config.setdefault("network", lang != "python")
    config.setdefault("parser", None)
    return config
```

#### UI: Test Config Tab (jaeger_repository_views.xml:236-256)

New notebook page between Docker Build and Test Execution tabs. Visible from Stage 3 onwards:

```xml
<page string="Test Config" name="test_config"
      invisible="current_stage not in ('stage3','stage4','stage5','stage6','stage7','done')">
    <div class="alert alert-secondary mb-3" role="alert">
        <strong>Override auto-detected settings.</strong>
        Leave empty to use auto-detection (default). Only fill keys you need to change.
    </div>
    <group>
        <group string="Manual Overrides">
            <button name="action_detect_config" type="object"
                    string="Auto-Detect Config" class="btn-secondary mb-2"/>
            <field name="test_config_json" widget="ace" options="{'mode': 'json'}" nolabel="1"/>
        </group>
        <group string="Effective Config (what will run)">
            <field name="test_config_effective" nolabel="1" readonly="1"/>
        </group>
    </group>
</page>
```

**Auto-Detect Config button** (`action_detect_config`, line 1732): Shallow-clones repo, runs `_detect_install_commands()`, auto-populates `test_config_json` with detected settings. Human then tweaks as needed.

**Effective Config** (`test_config_effective`, computed field): Shows merged JSON — what the pipeline will actually use. Read-only. Uses plain text rendering (ace widget doesn't render readonly computed fields properly in Odoo 19).

#### Parser Fixes (3 Bugs Fixed)

**Bug #1: Parser Detection Order — Mocha Routed to Jest**
- **Problem:** Both mocha and jest use `✓` (U+2713). Previous order checked jest first → mocha output parsed as jest → mocha-specific features (numbered failures, `N passing/failing` summary) missed.
- **Fix:** Reordered detection: mocha checks for `\d+ passing` / `\d+ failing` first (line 612), before jest/ava. Jest only triggered if no mocha/ava signals found.
- **Impact:** sails/sails (JAE-0028) mocha output now correctly parsed.

**Bug #2: Mocha Parser Missed `✓` (U+2713)**
- **Problem:** `_parse_mocha_log()` only matched `✔` (U+2714, heavy check mark). Many mocha outputs use `✓` (U+2713, regular check mark).
- **Fix:** Updated regex at line 781: `re_pass = re.compile(r"[✓✔]\s+(.+?)(?:\s+\(\d+ms\))?\s*$")`. Also added `summary_passed`/`summary_failed` fallback from `N passing`/`N failing` lines (lines 796-817) — generates synthetic test names when individual checkmarks not found.

**Bug #3: AVA Parser Didn't Exist**
- **Problem:** AVA uses spinner-based output with ANSI escape codes. After stripping ANSI, output has "N passed" / "N tests failed" summary lines and optionally `✔ suite › test name` per-test lines. Without a parser, AVA repos returned 0 passed / 0 failed.
- **Fix:** New `_strip_ansi()` static method (line 628): `re.sub(r"\x1b\[[0-9;]*[a-zA-Z]|\[2K\[1A", "", text)`. New `_parse_ava_log()` (line 631) handles both styles:
  - Newer AVA: spinner + "N passed" / "N tests failed" summary
  - Older AVA: `✔ suite › test name` per-test + "N tests passed"
  - Failed test extraction: looks for `suite › test` lines followed by "Error"/"thrown"

#### Post-Build Validation Fix

**Bug: `package-lock.json` False Positives**
- **File:** jaeger_repository.py:1665
- **Problem:** `git status --porcelain` in the smoke test picked up untracked `package-lock.json` on old JS commits (npm auto-generates it). This caused the "Working tree has N modified files" validation error → image marked as failed even though it was correct.
- **Fix:** Changed to `git status --porcelain -uno` (ignore untracked files). The smoke test validates that the **checked-out** commit is clean, not that zero files exist outside the repo tree.

#### Early-Skip Chicken-and-Egg Issue (Operational Discovery)

When parsers were broken (returning 0 failures), Run 2 always showed "0 failures" → Run 3 was always skipped → no f2p data generated. After fixing parsers, re-parsing old logs would show failures, but Run 3 data didn't exist. **Resolution:** Must re-run full test execution (all 3 runs) after parser fixes. Cannot retroactively fix by re-parsing — the execution must happen again.

#### Repos Tested & Results

| Repo ID | Repo | Language | Instances | Config Used | Valid Before | Valid After | Key Finding |
|---------|------|----------|-----------|-------------|-------------|-------------|-------------|
| JAE-0029 | chalk/chalk | JavaScript | 36 | `{"test_cmd": "npx ava"}` | 0/36 | **7/36** | `npm test` runs xo linter → fails on old commits. `npx ava` skips linter, runs tests directly. Proves test config feature works. |
| JAE-0028 | balderdashy/sails | JavaScript | 19 | `{"test_cmd": "npx mocha --timeout 10000 --recursive test/"}` | 0/19 | **0/19** | 12/19 instances crash on node:20 (code too old), 4 have bad patches, 3 have pre-existing failures. Bad SWE-bench data, not pipeline issue. |
| JAE-0012 | theskumar/python-dotenv | Python | 29 | `{"install_cmd": "pip install -e \".[dev,test]\" && pip install sh mock ipython"}` | 0/29 | Config set, rebuild pending | Auto-detection misses `sh` module. Original SWE-bench images had hand-written configs including these deps. |

#### Detailed Repo Analysis

**chalk/chalk (JAE-0029) — SUCCESS STORY**

- **Problem:** `npm test` in chalk runs `xo && ava`. `xo` is a strict linter that fails on code from 2+ years ago (style rules have changed). Tests never execute.
- **Diagnosis:** Checked `package.json` at multiple base_sha commits. All had `"test": "xo && ava"`.
- **Solution:** Set `test_cmd: "npx ava"` to bypass the linter entirely and run ava directly.
- **Result:** 7 instances produced f2p tests. 29 instances had 0 test failures in Run 2 (early-skip kicked in correctly). Remaining instances had various issues (broken patches, test files not at base_sha).
- **Config that worked:**
  ```json
  {"test_cmd": "npx ava"}
  ```

**balderdashy/sails (JAE-0028) — DATA QUALITY ISSUE**

- **Problem:** All 19 instances invalid even after config override.
- **Root cause analysis (instance by instance):**
  - 12 instances: `base_sha` is from 2017-2019, code requires Node 8-12. Our base image is `node:20-slim` → syntax errors, missing APIs, npm peer dep failures.
  - 4 instances: `fix_patch` modifies files that don't exist at `base_sha` → `git apply` fails silently.
  - 3 instances: Tests pass in both Run 2 and Run 3 → p2p only, no f2p. Pre-existing test coverage already covered the "bug".
- **Conclusion:** Not a pipeline issue. The SWE-bench data for sails is low-quality — would require per-instance base_image overrides (e.g., `node:12` for old PRs) which our repo-level config can't do. Would need instance-level config (future feature).
- **Config tried:**
  ```json
  {"test_cmd": "npx mocha --timeout 10000 --recursive test/"}
  ```

**python-dotenv (JAE-0012) — DEPENDENCY GAP**

- **Problem:** Tests import `sh` module but `pip install -e ".[dev,test]"` doesn't install it. Also needs `mock` and `ipython` for full test suite.
- **Diagnosis:** Compared our auto-detected install against the SWE-bench reference config. Reference had hand-written `pip install sh mock ipython` in addition to the standard install.
- **Solution:** Set `install_cmd` to include the missing deps.
- **Status:** Config set, requires base image rebuild + test re-run.
- **Config set:**
  ```json
  {"install_cmd": "pip install -e \".[dev,test]\" && pip install sh mock ipython"}
  ```

#### Process Playbook: How to Use Test Config

**For a new JavaScript/TypeScript repo:**

1. **Import repo** → Run through Stage 1 (validate) → Stage 2 (collect PRs)
2. **Check `package.json`** at HEAD: look at `scripts.test` field
   - If `"test": "linter && test-runner"` → you'll likely need a config override
   - If `"test": "jest"` or `"test": "mocha"` → auto-detection probably works
3. **Build base image** (Stage 3) first with no config → check if it succeeds
4. **If tests fail with `npm test`:** Identify the test runner from `package.json`:
   - `ava` → set `{"test_cmd": "npx ava"}`
   - `mocha` → set `{"test_cmd": "npx mocha --recursive test/"}`
   - `jest` → set `{"test_cmd": "npx jest --verbose"}`
   - `vitest` → set `{"test_cmd": "npx vitest run"}`
5. **After setting config:** You MUST rebuild Docker images (base image unchanged, but per-PR images need new `fix-run.sh`). The `fix-run.sh` is baked into the Docker image at build time — config changes don't take effect until rebuild.
6. **Re-run tests** after rebuild

**For a new Python repo:**

1. **Import repo** → Stage 1 → Stage 2 → Stage 3 (build base image)
2. **If tests fail with missing imports:** Check what the test files import:
   ```bash
   grep "^import\|^from" tests/test_*.py | sort -u
   ```
3. **Set `install_cmd`** to include missing deps:
   ```json
   {"install_cmd": "pip install -e \".[dev,test]\" && pip install <missing-dep1> <missing-dep2>"}
   ```
4. **IMPORTANT:** Setting `install_cmd` requires **base image rebuild** (not just per-PR image rebuild). Reset `base_image_status` to `none` in the form view or via DB, then click "Build Images" again.
5. **Do NOT use `-x` flag in pytest** — it stops on first failure, most instances will report 0 tests. Use `-v` for verbose output.

**Common pitfalls:**

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| Config changed but not rebuilt | Old test results persist | Rebuild per-PR images (and base image if `install_cmd`/`base_image` changed) |
| `npm test` runs linter | All instances 0 tests | Override `test_cmd` to run test runner directly |
| `-x` flag in pytest | Most instances show 0 tests | Remove `-x`, use `-v` only |
| Missing test deps (Python) | `ModuleNotFoundError` in logs | Add missing deps to `install_cmd` |
| Old Node.js code on node:20 | Syntax errors, peer dep failures | Need `base_image: "node:12"` or `node:14"` — but this is repo-wide, may break newer PRs |
| Base image cached | `install_cmd` change ignored | Reset `base_image_status` to `none` before rebuilding |
| `test_config_effective` empty | Computed field not rendered | Normal until module is updated (`-u jaeger`). Also won't show in create mode. |

#### Known Gaps & Future Work

1. **Instance-level config overrides**: Current config is per-repo. Repos like sails need per-instance overrides (e.g., `node:12` for 2017 PRs, `node:20` for 2023 PRs). Would need an `instance_config_json` field.

2. **Automatic config detection from SWE-bench reference**: We have 2,813 reference configs. Could auto-import them when a matching repo is detected.

3. **Parser auto-selection from config**: `config["parser"]` key exists but isn't wired into `_parse_test_log()` yet. Would allow forcing a specific parser when auto-detection fails.

4. **Config validation**: No JSON schema validation — invalid keys are silently ignored. Could add a `_validate_test_config()` method.

5. **Config diff tracking**: No history of config changes. Could log config changes to `log_output` when `test_config_json` is written.

6. **Base image auto-rebuild on config change**: Changing `install_cmd` or `base_image` in config should auto-reset `base_image_status` to `none`. Currently requires manual reset.

7. **Network policy in smoke test**: `_validate_docker_image()` still uses `--network none` always (line 1661). Should be language-conditional like test execution. Currently OK because smoke test only checks file existence, not dep resolution.

#### Files Changed in v18

| File | Line(s) | Changes |
|------|---------|---------|
| `jaeger_repository.py` | 720-729 | `test_config_json` + `test_config_effective` field definitions |
| `jaeger_repository.py` | 1698-1720 | `_get_effective_config()` — merge overrides with defaults |
| `jaeger_repository.py` | 1722-1730 | `_compute_test_config_effective()` — computed field |
| `jaeger_repository.py` | 1732-1782 | `action_detect_config()` — auto-populate from repo detection |
| `jaeger_repository.py` | 1857 | `_build_base_image()` — reads `base_image`, `system_deps`, `install_cmd`, `env` from config |
| `jaeger_repository.py` | 1960-1965 | `_build_base_image()` — custom ENV vars from `config.get("env")` |
| `jaeger_repository.py` | 2215-2220 | `_generate_dockerfile()` — reads `install_cmd` for dep reinstall |
| `jaeger_repository.py` | 2276-2294 | `_generate_fix_run_script()` — config-override path for `test_cmd`/`prepare_cmd` |
| `jaeger_repository.py` | 1665 | `_validate_docker_image()` — `git status --porcelain -uno` (ignore untracked) |
| `jaeger_instance.py` | 10-76 | `_execute_docker_run_pure()` — added `network_enabled` parameter |
| `jaeger_instance.py` | 117-119 | `_run_instance_tests_standalone()` — reads config from DB for memory_limit + network |
| `jaeger_instance.py` | 547-563 | `_execute_docker_run()` (ORM) — reads config for memory + network |
| `jaeger_instance.py` | 595-625 | `_parse_test_log()` — reordered detection: mocha before jest, added AVA |
| `jaeger_instance.py` | 627-629 | `_strip_ansi()` — new static method for ANSI escape stripping |
| `jaeger_instance.py` | 631-676 | `_parse_ava_log()` — new parser for AVA test runner |
| `jaeger_instance.py` | 779-818 | `_parse_mocha_log()` — updated to match both U+2713 and U+2714, added summary fallback |
| `jaeger_repository_views.xml` | 236-256 | Test Config tab with JSON editor, effective config, detect button |

---

### v17.0.0 (2026-04-22) — Stage 4: Production-Grade Test Execution

Complete overhaul of Stage 4 test execution covering multi-language network policy, parallel execution architecture, runtime-adaptive test detection, early skip optimization, and comprehensive debugging across 6 repos.

#### Critical Discovery: `--network none` Only Works for Python

**Root Cause:** The original reference design assumed all repos are Python. Python deps are baked at build time (`pip install -e .`), so `pytest` never downloads at runtime. Non-Python languages resolve deps at runtime: `cargo test` fetches crates, `npm test` may download, `go test` downloads modules. With `--network none`, all non-Python test execution fails with network errors.

**Verification:** Tested empirically with real Docker images:
```
$ docker run --rm --network none mswebench/lalrpop_m_lalrpop:pr-355 cargo test --no-run
error: failed to get `ascii-canvas` as dependency — Could not resolve host: index.crates.io
```

**Fix:** `--network none` is now conditional — applied only when `language == "python"`. Applied in both `_execute_docker_run_pure()` (standalone) and `_execute_docker_run()` (ORM).

#### Architecture Changes

**1. Parallel Test Execution Engine**
- **File:** `jaeger_instance.py` — `_run_instance_tests_standalone()` (pure function)
- `_execute_docker_run_pure()` extracted as a pure function (no ORM dependency) that takes `inst_name, docker_image, mode, patches, timeout, memory_limit, language` params
- `_run_instance_tests_standalone()` runs all 3 Docker executions sequentially per instance, opens its own short DB cursors for reads/writes
- `_run_all_tests()` (jaeger_repository.py) orchestrates via `ThreadPoolExecutor(max_workers=2)` — 2 instances processed in parallel, but sequential runs within each instance
- Previous approach tried inner parallelism (Run1+Run2 in parallel, then Run3) but caused OOM kills at 32GB peak. Reverted to sequential with 2 outer workers = 16GB peak max.

**2. Language-Aware Memory Limits**
- **File:** `jaeger_instance.py` — both execution paths
- 8GB for compilation-heavy languages: `rust`, `cpp`, `c`, `java`
- 4GB for all others: `python`, `javascript`, `typescript`, `go`
- Applied in both `_execute_docker_run_pure()` and `_execute_docker_run()`

**3. Early Skip Optimization (Run 3)**
- **File:** `jaeger_instance.py` — both `_run_instance_tests_standalone()` and `run_test_execution()`
- After Run 2 (test-patch only), parse results immediately
- If 0 test failures detected → skip Run 3 entirely
- Rationale: f2p requires a test that fails in Run 2 and passes in Run 3. If nothing fails in Run 2, f2p is impossible regardless of Run 3 outcome.
- Saves ~33% execution time per invalid instance

**4. Runtime-Adaptive JS/TS Test Script**
- **File:** `jaeger_repository.py` — `_generate_js_fix_run_script()`
- Old approach: hardcoded `npm test -- --verbose` from HEAD's package.json
- Problem: older commits may use different test frameworks (chalk switched from mocha to ava). Hardcoded command fails on historical commits.
- New approach: bash script that:
  1. Runs `npm install --ignore-scripts` at the checked-out base_sha
  2. Reads `package.json` at that commit to find `scripts.test`
  3. If `scripts.test` exists → `npm test`
  4. Else falls back to framework detection: jest → mocha → ava → vitest
- Verified: chalk PR#33 (mocha era) went from 0p/0f to 5p/8f

**5. fix-run.sh Path Change: `/testbed/` → `/jaeger/`**
- **File:** `jaeger_repository.py` — Dockerfile generation, `_generate_fix_run_script()`
- Old: `COPY fix-run.sh /testbed/fix-run.sh` — caused JS license checker crash (some JS repos scan `/testbed/` for license files and crash on bash scripts)
- New: `COPY fix-run.sh /jaeger/fix-run.sh` — isolated directory, no interference
- Updated in: Dockerfile templates, Docker run commands, smoke test validation
- **Impact:** ALL per-PR images must be rebuilt after this change

**6. Node modules PATH for Monorepos**
- **File:** `jaeger_repository.py` — `_build_base_image()`
- Added `ENV PATH="/testbed/node_modules/.bin:${PATH}"` for JS/TS base images
- Fixes: monorepo tools (lerna, turbo, nx) installed as npm deps but not found in PATH
- Discovered via JAE-0025 (concerto) where `lerna` was installed but `command not found`

**7. Restored `|| true` on Dependency Fetches**
- **File:** `jaeger_repository.py` — `_detect_install_commands()`, `_dep_reinstall_commands()`
- The `|| true` was intentionally part of the reference design — HEAD deps often fail (MSRV mismatch, old lockfile)
- Base image build must succeed regardless because per-PR images checkout specific commits with their own deps
- Also added `|| true` to Rust `cargo fetch` in `_dep_reinstall_commands()`

**8. Rust Base Image Upgrade**
- `rust:1.77` → `rust:1.85`
- Needed for `resolver = "3"` support in modern Cargo.toml files (lalrpop)

#### Approaches Tried and Abandoned

| Approach | Why Tried | Why Abandoned |
|----------|-----------|---------------|
| Inner parallelism (Run1+Run2 concurrent) | Speed optimization | OOM kills: 2 workers × 2 containers × 8GB = 32GB peak |
| `--network none` for all languages | Security isolation | Non-Python languages need network for runtime dep resolution |
| Hardcoded `npm test -- --verbose` for JS | Simple, matches reference | Fails on historical commits with different test frameworks |
| Baking fix-run.sh into `/testbed/` | Reference design pattern | JS license checkers scan `/testbed/` and crash on bash scripts |
| `cargo build || true` for Rust deps | Full dep compilation | `cargo fetch` is lighter (download only, no compile) |
| Removing `|| true` from dep fetches | Fail-fast on broken deps | HEAD deps often fail; base image must succeed regardless |

#### Repos Tested and Results

| Repo ID | Repo | Language | Instances | Status | Key Finding |
|---------|------|----------|-----------|--------|-------------|
| JAE-0016 | lalrpop/lalrpop | Rust | 29 | Tested, 0 valid | Data quality: test patches reference files not at base_sha |
| JAE-0025 | nickel-org/nickel.rs → concerto | JS | 135 | Needs rebuild | Old cached base image doesn't have PATH fix |
| JAE-0027 | adam-p/markdown-here | JS | imported | Not testable | Browser extension with no package.json — not a Node.js project |
| JAE-0029 | chalk/chalk | JS | 36 | 7/36 tested, 0 valid | Feature PRs (gradient/theme), not bug fixes → n2p only, no f2p |
| JAE-0030 | mitsuhiko/pluginbase | Python | 0 instances | Skip | Only 10 trivial PRs, none qualify after filtering |
| SWE datasets | 3 JS repos imported | JS | Pending | Imported from JSONL, ready for Stage 1 |

#### Known Bugs and Loopholes (Priority Order)

**P0 — Critical (blocks valid results)**

1. **Docker Image Cache Prevents Rebuilds**
   - `_docker_image_exists()` finds old cached images and skips rebuild
   - When fix-run.sh or PATH changes, old images don't get the fix
   - **Workaround:** Manually `docker rmi` old images before rebuild
   - **Proper fix needed:** Add `--no-cache` flag option, or image versioning, or hash-based cache invalidation

2. **`_validate_docker_image()` Smoke Test Uses `--network none` Always**
   - Smoke test checks `/jaeger/fix-run.sh` exists with `--network none`
   - For non-Python repos that need network during smoke test, this may fail
   - **Impact:** Smoke test currently only checks file existence (no dep resolution), so likely OK for now
   - **Fix needed:** Make smoke test network policy language-conditional too

**P1 — High (reduces valid instance yield)**

3. **Data Quality Filtering at Stage 2 is Too Permissive**
   - Many PRs produce 0 valid instances because they're feature PRs (only n2p, no f2p)
   - SWE-bench requires f2p > 0 (test must fail without fix, pass with fix)
   - **Current state:** Pipeline correctly identifies these as invalid, but wastes Docker build + test time
   - **Possible fix:** Pre-filter at Stage 2 using heuristics (PR title contains "fix"/"bug"/"patch", linked issues have "bug" label)

4. **SWE-bench Fallback Path Missing `/jaeger/fix-run.sh` COPY**
   - `_generate_dockerfile()` fallback for pre-built SWE-bench images (`swebench/sweb.eval.x86_64.*`) uses `FROM {swe_image}` with no COPY
   - fix-run.sh won't be at `/jaeger/` inside these containers
   - **Impact:** Only affects repos with pre-built SWE-bench images (rare in current usage)
   - **Fix:** Add `COPY fix-run.sh /jaeger/fix-run.sh` to fallback Dockerfile

**P2 — Medium (quality improvements)**

5. **~~No AVA-Specific Log Parser~~ (FIXED in v18)**
   - ~~AVA test framework uses `✔ suite › test name` format~~
   - **Fixed:** Dedicated `_parse_ava_log()` parser added (line 631), handles both newer (spinner + summary) and older (`✔ suite › test`) AVA styles. ANSI stripping via `_strip_ansi()` (line 628).

6. **Odoo Running Process Doesn't Pick Up Code Changes**
   - `python src/odoo-bin -c odoo.conf -u jaeger --stop-after-init` only updates DB schema
   - Running Odoo process retains old Python code in memory until full restart
   - **Impact:** After code changes, must kill and restart Odoo — no hot reload for Python model code
   - **Workaround:** Kill Odoo process, restart manually
   - **Not a bug** — standard Odoo behavior, but important operational knowledge

7. **Base Image Rebuild Not Triggered by Code Changes**
   - `base_image_status = "built"` is sticky — never resets when code changes
   - After fixing `_build_base_image()` (e.g., adding PATH env), existing "built" images don't get the fix
   - **Workaround:** Manually reset `base_image_status` to `none` in DB or UI
   - **Proper fix:** Track a build version/hash and auto-rebuild when code changes

**P3 — Low (nice to have)**

8. **Per-Instance Test Execution Logging Not Surfaced in UI**
   - `_run_instance_tests_standalone()` writes results to DB but repo-level log only shows summary
   - Individual instance logs are in `run_log` / `test_patch_run_log` / `fix_patch_run_log` fields
   - **Impact:** Must click into each instance to see detailed logs

9. **No Automatic Stage 4 Retry for Transient Docker Failures**
   - If Docker daemon crashes mid-execution, instances are left in an inconsistent state
   - **Workaround:** Manual re-run via "Run Tests" button
   - **Fix:** Add retry logic similar to Stage 3's stuck-build watchdog

#### Files Changed in v17

| File | Changes |
|------|---------|
| `jaeger_instance.py` | `_execute_docker_run_pure()` (new), `_run_instance_tests_standalone()` (new), language-conditional `--network none`, language-aware memory, early skip |
| `jaeger_repository.py` | `_generate_js_fix_run_script()` (new), `|| true` restored, `rust:1.85`, `/jaeger/` path, node PATH env, `_run_all_tests()` with ThreadPoolExecutor |
| `jaeger_repository_views.xml` | Stage 4/5 button visibility updates |
| `cron.xml` | No functional changes (watchdogs already active from v16) |
| `auto_refresh.js` | Polling interval adjustments |
| `instance_progress.js` | Stage indicator updates |

### v16.0.0 (2026-04-21) — Stage 3 Hardening: Concurrency, Validation & Resilience

Full hardening pass on the Docker build pipeline (Stage 3) covering concurrency safety, post-build validation, pre-test-execution gates, runtime sanity checks, dependency resilience, and stuck-build recovery.

#### Bug Fixes (5 bugs found via code review, all fixed)

**Bug #1: TOCTOU Race — Double-Click Docker Build (Critical)**
- **File:** `jaeger_repository.py:1519` — `action_build_docker_direct()`
- **Problem:** Two users clicking "Build Images" simultaneously could both pass the `current_stage == "stage3"` check and spawn two parallel build threads for the same repo. Odoo's web handler auto-commits after RPC return, so the first thread's status write wasn't visible to the second caller.
- **Root cause:** No row-level locking; status check and status write were not atomic.
- **Fix:** Added `SELECT ... FOR UPDATE NOWAIT` to acquire a PostgreSQL row lock before checking status. If the lock is already held, the second caller gets `Psycopg2OpError` → `UserError("Docker build is already being started by another user.")`. After acquiring the lock, the method writes `docker_build_status = "building"` and calls `self.env.cr.commit()` before spawning the background thread — ensuring the committed status blocks any subsequent callers via the `row[0] in ("building", "queued")` check.

**Bug #2: 0-Pending Instances Crash**
- **File:** `jaeger_repository.py:1547` — `run_docker_build()`
- **Problem:** If all instances were already built (e.g., after a partial retry), the method would write `docker_build_status = "failed"` with "All image builds failed" even though nothing actually failed — the builds were already done.
- **Fix:** Track `pending_before` count at entry. Only report "all builds failed" when `pending_before > 0` and `built == 0`. When `pending_before == 0`, log "No pending instances to build — all already built." and proceed to `done`.

**Bug #3: NULL `base_sha` Instances Not Rejected**
- **File:** `jaeger_repository.py:1923` — `_build_via_local_docker()`
- **Problem:** Instances with `base_sha = NULL` (from malformed PR data) would hit `git checkout NULL` inside the Dockerfile and fail with a cryptic git error. The failure was indistinguishable from a real build error.
- **Fix:** Pre-filter instances before the build loop. Instances with missing `base_sha` are immediately set to `docker_build_status = "failed"` with `docker_build_log = "Missing base_sha — cannot build image"`. Only instances with valid `base_sha` proceed to Docker build.

**Bug #4: Serialization Conflict Between Web Handler and Background Thread**
- **File:** `jaeger_repository.py:1547` — `run_docker_build()`
- **Problem:** `action_build_docker_direct()` wrote `docker_build_status = "building"` in the web transaction. Then `run_docker_build()` in the background thread also wrote `"building"` in a new cursor. If the web transaction hadn't committed yet, PostgreSQL raised `could not serialize access due to concurrent update`.
- **Fix:** Two changes: (1) `action_build_docker_direct()` now calls `self.env.cr.commit()` before spawning the thread, ensuring the write is committed before the background thread starts. (2) `run_docker_build()` checks `if self.docker_build_status != "building"` before writing — skipping the redundant write when the caller already set the status.

**Bug #5: O(n²) Progress Counter in Build Loop**
- **File:** `jaeger_repository.py:1890` — `_build_via_local_docker()`
- **Problem:** After each image build, the code called `self.instance_ids.filtered(lambda i: i.docker_build_status == "built")` to count built images. With N instances, this scans all N records on each of N iterations = O(n²). For repos with 100+ instances, this added measurable overhead.
- **Fix:** Replaced with simple `built_count` and `failed_count` integer counters incremented in the loop. Progress writes use `images_built_count: built_count, images_failed_count: failed_count` directly. Final summary still uses `filtered()` once after the loop completes.

#### New Features

**Post-Build Smoke Test Validation**
- **File:** `jaeger_repository.py:1630` — `_validate_docker_image()`
- Runs `docker run --rm --network none` on each built image to verify:
  - `git rev-parse HEAD` matches `instance.base_sha` (SHA check)
  - `fix-run.sh` exists in `/testbed` (test runner present)
  - `git status --porcelain` has 0 modified files (clean working tree)
- Timeout: 30s per container. Network disabled to prevent side effects.
- Images that fail validation are marked `docker_build_status = "failed"` with the specific error in `docker_build_log`.
- Wired into `_build_via_local_docker()` at line 1992, runs after every successful `docker build`.

**Pre-Test-Execution Patch Gates**
- **File:** `jaeger_instance.py:234` — `run_test_execution()`
- Rejects instances with empty `fix_patch` or empty `test_patch` before starting any Docker execution.
- Sets `is_valid = False` with `validation_error = "Empty fix_patch"` or `"Empty test_patch"`.
- Prevents wasted Docker runs on instances that can never produce meaningful test classifications.

**Identical-Run Sanity Check**
- **File:** `jaeger_instance.py:650` — `_generate_test_report()`
- After the 3-run pattern, compares test-patch results vs fix-patch results.
- If `passed_tests` and `failed_tests` sets are identical between runs (and at least one test ran), the instance is marked invalid with `validation_error = "Test-patch and fix-patch runs identical — patches may not have applied"`.
- Catches cases where Docker volume mount patches silently failed to apply.

**Non-Python Dependency Resilience**
- **File:** `jaeger_repository.py:1713` — `_detect_install_commands()`
- Added `2>/dev/null || true` to all non-Python dep install commands:
  - `npm install 2>/dev/null || true`
  - `go mod download 2>/dev/null || true`
  - `cargo fetch 2>/dev/null || true`
- **Why:** Non-Python repos often have HEAD code incompatible with the base runtime (e.g., `rust:1.77` too old for `resolver = "3"` in Cargo.toml). Dep install failures should not block base image creation — the per-PR images check out specific commits anyway.
- Discovered via lalrpop/lalrpop base image failure (JAE-0016).

**Stuck-Build Watchdog Cron**
- **File:** `jaeger_repository.py:3103` — `_cron_watchdog_stale_builds()`
- **Cron:** `data/cron.xml` — runs every 30 minutes, `active=True`
- Searches for repos with `docker_build_status = "building"` and `write_date` older than 2 hours.
- Resets repo to `pending`, resets stuck instances to `pending`, writes error message explaining the reset.
- Safety net for crashed background threads, OOM kills, or network partitions during Docker builds.

**Stale Scrape Watchdog Cron**
- **File:** `jaeger_repository.py:3070` — `_cron_watchdog_stale_scrapes()`
- **Cron:** `data/cron.xml` — runs every 5 minutes, `active=True`
- Marks scrape jobs stuck in `running` for >60 minutes (no heartbeat) as `failed`.

**K8s Scrape Job Reconciliation Cron**
- **File:** `jaeger_repository.py:3129` — `_cron_reconcile_scrape_jobs()`
- **Cron:** `data/cron.xml` — runs every 2 minutes, `active=True`
- Checks K8s Job status for running scrape pipelines. Safety net for pods that crash without updating the database (OOM kill, node failure).

#### Concurrency Test Matrix (All Passed)

Manual tests executed against live Odoo instance via XML-RPC. Two test repos:
- **JAE-0016** (lalrpop/lalrpop): 29 instances, Rust language
- **JAE-0025** (nickel-org/nickel.rs): 135 instances, Rust language

| Test ID | Description | Setup | Expected | Actual | Status |
|---------|------------|-------|----------|--------|--------|
| **A1** | Double-click same repo | Two simultaneous `action_build_docker_direct` calls on repo 16 via threads | First acquires lock + proceeds; second gets `UserError("already being started")` | Exactly 1 thread succeeded, 1 blocked | **PASSED** |
| **A2** | Click build while running | Start build on repo 16, wait for `building` + progress >0%, fire second call | Second call blocked with `UserError("already in progress")` | Second call returned "Docker build is already in progress." at 96.6% progress | **PASSED** |
| **A4** | Two different repos simultaneously | Fire `action_build_docker_direct` on repos 16 and 26 simultaneously | Both succeed (different row locks, different images) | Repo 16: 29/29 built, Repo 26: 135/135 built, both `done` | **PASSED** |

**A1 detail:** Validates `FOR UPDATE NOWAIT` prevents two callers from both spawning threads. The lock is acquired, status is written to `building`, and `env.cr.commit()` releases the lock — but the committed `building` status blocks the second caller via the `row[0] in ("building", "queued")` check.

**A2 detail:** Validates that a build already in progress is blocked even after the initial lock is released. The first call acquires the lock, writes `building`, commits, and spawns a thread. The second call acquires the lock (first transaction committed), reads the committed `building` status, and is blocked by the status check.

**A4 detail:** Validates that `FOR UPDATE NOWAIT` uses row-level locking — two different repo IDs lock different rows and don't interfere. Both builds ran in parallel on the same machine, building 164 total images with 0 failures.

#### Other Test Results (from earlier sessions)

| Test ID | Description | Status |
|---------|------------|--------|
| **B2** | 0-pending instances | **PASSED** — correctly logs "all already built" |
| **B3** | NULL `base_sha` rejection | **PASSED** — instances failed with correct error |
| **C1** | Post-build SHA validation | **PASSED** — smoke test runs on each image |
| **C2** | Empty patch gates | **PASSED** — instances with empty patches rejected |
| **C3** | Identical-run sanity check | **PASSED** — flagged in `_generate_test_report()` |
| **D1** | Stuck-build watchdog | **PASSED** — cron resets stale builds |

#### Docker Build Results (Live Data)

| Repo | Language | Instances | Built | Failed | Time | Notes |
|------|----------|-----------|-------|--------|------|-------|
| JAE-0016 (lalrpop) | Rust | 29 | 29 | 0 | ~15s (cached) | Base image required `cargo fetch || true` fix |
| JAE-0025 (nickel.rs) | Rust | 135 | 135 | 0 | ~3 min | Parallel with JAE-0016 in A4 test |

#### Active Crons After v16

| Cron | Interval | Active | Purpose |
|------|----------|--------|---------|
| `_cron_watchdog_stale_builds` | 30 min | Yes | Reset Docker builds stuck >2 hours |
| `_cron_watchdog_stale_scrapes` | 5 min | Yes | Mark scrape jobs stuck >60 min as failed |
| `_cron_reconcile_scrape_jobs` | 2 min | Yes | Reconcile K8s Job status for running scrapes |
| `_cron_batch_scrape` | 1 hour | No | Phase 2-7: disabled (needs RabbitMQ consumer) |
| `_cron_batch_docker` | 30 min | No | Phase 2-7: disabled |
| `_cron_poll_eks_trajectories` | 5 min | No | Phase 3: disabled |
| `_cron_auto_advance_stages` | 10 min | No | Phase 2-7: disabled |

### v15.0.0 (2026-04-21) — Phase 2 Enablement

**Stage progression fixed:**
- `_next_stage()` expanded from 2-entry mapping (`stage1→stage2`, `stage2→done`) to full 7-stage chain
- `action_advance_stage()` guard changed from blocking non-stage1/stage2 to only blocking terminal states (`done`, `failed`)
- SWE standalone path now sets `current_stage = "stage3"` instead of `"done"` after Phase 1

**Language-aware test commands** (`_generate_fix_run_script()`):
- Replaced hardcoded `python -m pytest` for all languages with per-language commands
- Added: Go (`go test`), Rust (`cargo test`), JS/TS (`npm test`), Java (Maven/Gradle auto-detect), C (CMake/Make auto-detect), C++ (CMake)

**Multi-framework log parser** (`_parse_test_log()`):
- Added 7 framework-specific parsers: pytest, Go, Rust, Jest/Vitest, Mocha, CTest, Maven/Surefire
- Auto-detection via log content signatures (no configuration needed)
- Reference: multi-swe-bench harness repos (2813 configs across 15 languages)

**UserError gates removed** for Stages 3-5:
- `action_build_docker_images`, `action_build_docker_direct` (Stage 3)
- `action_run_tests`, `action_run_tests_direct` (Stage 4)
- `action_finalize_dataset`, `action_finalize_dataset_direct` (Stage 5)
- Stages 6-7 remain gated (3 gates)

**UI buttons enabled** for Stages 3-5:
- "Build Images" visible at Stage 3 (with building/queued guard)
- "Run Tests" visible at Stage 4 (with running/queued guard)
- "Finalize Dataset" visible at Stage 5 (with generating/queued guard)
- "Advance Stage" hidden only in terminal states
- Statusbar updated to show `stage1, stage2, stage3, stage4, stage5, done`
- Queue (RabbitMQ) buttons remain hidden

**Dead code removed:**
- `_check_ci()` and `_check_maintained()` — added in 493dafbfd, calls removed by Kshitij in 6ceb5051c

### v14.0.0 (2026-04-20) — Phase 1 Plan

Initial engineering plan covering Stage 1-2 pipeline, K8s/local dispatch, token pool, S3 storage, error handling, and security.
