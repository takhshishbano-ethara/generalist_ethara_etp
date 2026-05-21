# Leviathan — Scalability Issues & Fixes

> **Module**: `custom_addons/leviathan/` (Odoo 19)  
> **Version**: 19.0.3.0.0  
> **Date**: May 2026  
> **Status**: 4 confirmed bottlenecks, 2 initially suspected but already mitigated in code

---

## Table of Contents

- [Executive Summary](#executive-summary)
- [How It Works (Quick Overview)](#how-it-works-quick-overview)
- [Issue 1: DB Cursor Pool Exhaustion (Critical)](#issue-1-db-cursor-pool-exhaustion-critical)
- [Issue 2: Unbounded Thread Pool Queue (Critical)](#issue-2-unbounded-thread-pool-queue-critical)
- [Issue 3: Bedrock Semaphore Per-Process (Critical)](#issue-3-bedrock-semaphore-per-process-critical)
- [Issue 4: Batch 250-Job Timing Bomb (Significant)](#issue-4-batch-250-job-timing-bomb-significant)
- [Issue 5: Bus Notification Storm (Minor)](#issue-5-bus-notification-storm-minor)
- [The Cascade Pattern (How It Actually Crashes)](#the-cascade-pattern-how-it-actually-crashes)
- [Proposed Fixes](#proposed-fixes)
- [Implementation Priority](#implementation-priority)
- [Appendix: Already Mitigated (Not Issues)](#appendix-already-mitigated-not-issues)

---

## Executive Summary

Leviathan works smoothly at low concurrency (10-20 parallel jobs) but breaks down at scale (200+ concurrent jobs). The module's own source code documents this — a comment on `leviathan_job.py` line 22-24 reads:

> *"3 pods x 4 Odoo workers x _PRD_POOL_SIZE=50 ... gives you 600 PRD threads system-wide, each grabbing DB cursors — which is exactly the cursor-pool exhaustion you saw at 200-concurrent."*

**4 confirmed bottlenecks** cause a cascading failure that doesn't just crash Leviathan — it freezes the entire Odoo instance (HR, Sales, Accounting, everything).

| # | Issue | Severity | Code Evidence |
|---|-------|----------|---------------|
| 1 | DB Cursor Pool Exhaustion | Critical | `leviathan_job.py` lines 15-91, 94-156, 2534-2550 |
| 2 | Unbounded Thread Pool Queue | Critical | `leviathan_job.py` lines 56-91 |
| 3 | Bedrock Semaphore Per-Process | Critical | `bedrock_service.py` lines 39-102 |
| 4 | Batch 250-Job Timing Bomb | Significant | `leviathan_job.py` lines 1202-1238 |
| 5 | Bus Notification Storm | Minor | `leviathan_job.py` (4 sites), `controllers/main.py` (1 site) |

---

## How It Works (Quick Overview)

```
1. Admin imports URLs (CSV)           → Jobs created in "not_assigned" state
2. Tasker claims a job                → State: "draft"
3. Tasker clicks Run                  → State: "extracting"
4. AWS Lambda extracts website data   → (9-phase pipeline, headless browser)
5. Lambda POSTs webhook to Odoo       → State: "generating"
6. Background thread: Bedrock LLM     → Generates PRD document (up to 3 attempts)
7. Scoring service grades the PRD     → 11-section rubric, 100 points
8. QC service checks for hallucinations → State: "scoring" → "done"
9. Tasker reviews and submits         → State: "submitted"
```

The issues occur in steps 5-8, where background threads compete for shared resources.

---

## Issue 1: DB Cursor Pool Exhaustion (Critical)

### What It Is

Every background thread needs a database cursor (connection) to write state updates. Odoo has a limited pool of cursors (`db_maxconn`, default 64 per worker). When background threads exceed this limit, the entire Odoo instance freezes.

### The Math

| Component | Cursor Demand | Source |
|-----------|--------------|--------|
| PRD generation threads | 50 per worker (4-5 cursors each per run) | `_PRD_POOL_SIZE=50` |
| Heartbeat daemon threads | 50 per worker (1 cursor every 60s each) | `_HeartbeatTicker`, 1 per active job |
| **Total demand** | **100 threads competing** | — |
| **Available cursors** | **64 per worker** | `db_maxconn` default |

### Why It Crashes Everything

The cursor pool is **shared across all of Odoo**, not just Leviathan. When 100 Leviathan threads hold all 64 cursors:
- Form views stop loading
- List views time out
- Cron jobs stall
- Other modules (HR, Sales, Accounting) freeze
- The entire Odoo instance becomes unresponsive

### Code Evidence

**Thread pool definition** (`leviathan_job.py` lines 15-28):
```python
_PRD_POOL_SIZE = int(os.environ.get("LEVIATHAN_PRD_POOL_SIZE", "50"))

# Per-pid pool registry. ... With 3 pods x 4 Odoo workers x
# _PRD_POOL_SIZE=50, the latter silently gives you 600 PRD threads
# system-wide, each grabbing DB cursors for heartbeats — which is
# exactly the cursor-pool exhaustion you saw at 200-concurrent.
```

**HeartbeatTicker** (`leviathan_job.py` lines 94-156): One daemon thread per active job, pulsing a cursor every 60 seconds:
```python
class _HeartbeatTicker:
    # ...
    def _run(self):
        while not self._stop_event.wait(self._interval):
            self._model._write_with_cursor(...)  # opens a new cursor each pulse
```

**Cursor acquisition** (`leviathan_job.py` lines 2534-2550):
```python
def _write_with_cursor(self, db_name, record_id, vals):
    with Registry(db_name).cursor() as cr:  # takes from shared Odoo pool
        # ...
```

### System-Wide Impact

```
System-wide threads = _PRD_POOL_SIZE x odoo_workers x pods
Example: 50 x 4 x 3 = 600 PRD threads + 600 heartbeat threads = 1,200 threads
```

---

## Issue 2: Unbounded Thread Pool Queue (Critical)

### What It Is

When all 50 PRD threads are busy, new tasks queue indefinitely. There is no maximum queue depth, no rejection policy, and no backpressure mechanism.

### What Happens

1. 250 webhooks arrive (from a batch run)
2. 50 threads start working immediately
3. 200 tasks queue in an unbounded `Queue` object
4. Each queued task holds its closure data in memory (extraction data, screenshot references)
5. Memory grows linearly with queue depth
6. The webhook already returned 200 OK to Lambda — the caller thinks work started
7. Watchdog sees queued jobs with stale heartbeats (for `extracting` state) and may retry them, adding more to the queue

### Code Evidence

**No rejection policy** (`leviathan_job.py` lines 56-91):
```python
def _submit_bg(label, fn, *args, **kwargs):
    pool = _get_pool()
    # Only a WARNING log when saturated — no actual rejection
    qsize = pool._work_queue.qsize()
    if qsize > _PRD_POOL_SIZE:
        _logger.warning(
            "PRD pool saturated: %d queued / %d workers — jobs "
            "will run but are delayed",
            qsize, _PRD_POOL_SIZE,
        )
    # Submits regardless of queue depth
    return pool.submit(_guarded)
```

### Memory Impact

Each queued task closure holds references to extraction data. At 200 queued tasks, this can consume significant memory that won't be freed until the task completes or the process restarts.

---

## Issue 3: Bedrock Semaphore Per-Process (Critical)

### What It Is

AWS Bedrock (the LLM API) has a rate limit (TPS quota). The code uses a `threading.Semaphore(5)` to throttle calls — but this semaphore is **per-process**, not per-cluster. Each Odoo worker process has its own independent semaphore, so the cluster-wide concurrency is `5 x total_workers`, which can exceed the AWS quota.

### The Math

| Component | Value |
|-----------|-------|
| Per-process semaphore | 5 concurrent calls |
| Odoo workers per pod | 4 (typical) |
| Pods | 3 (typical) |
| **Cluster-wide concurrent calls** | **5 x 4 x 3 = 60** |
| **Typical AWS Bedrock quota** | **20-50 TPS** |

At 60 concurrent calls against a 30 TPS quota, Bedrock returns 429 (rate limit). The adaptive retry mechanism then queues each call for 5-30 minutes. Threads hold cursors while waiting — **this directly worsens Issue 1**.

### Code Evidence

**Per-process semaphore** (`bedrock_service.py` lines 39-59):
```python
# Per-process (not per-pid, not per-cluster) — each Odoo worker
# process has its own semaphore.
_BEDROCK_MAX_CONCURRENT = int(
    os.environ.get("LEVIATHAN_BEDROCK_MAX_CONCURRENT", "5")
)
_BEDROCK_SEMAPHORE = threading.Semaphore(_BEDROCK_MAX_CONCURRENT)
```

**30-minute timeout** (`bedrock_service.py` lines 68-102):
```python
@contextmanager
def _bedrock_slot(call_label="bedrock"):
    acquired = _BEDROCK_SEMAPHORE.acquire(timeout=1800)  # 30 min wait!
```

### Cascade Effect

When Bedrock throttles:
1. Threads wait up to 30 minutes for a semaphore slot
2. While waiting, they may still hold DB cursors (depending on where in the code they are)
3. New Bedrock calls from other workers pile up independently (no coordination)
4. All of this pressure feeds back into Issue 1 (cursor exhaustion)

---

## Issue 4: Batch 250-Job Timing Bomb (Significant)

### What It Is

The "Run Batch (Parallel)" server action (`action_run_batch_concurrent`) fires ALL selected jobs at once using a temporary `ThreadPoolExecutor(250)`. When 250 Lambda invocations complete, ~250 webhooks arrive within a 5-15 minute window, overwhelming the PRD thread pool.

### The Timeline

```
T+0 min:    Admin clicks "Run Batch" on 250 jobs
T+0-1 min:  250 Lambda invocations fire (fast, async)
T+5-15 min: 250 webhooks arrive (Lambda timeout = 15 min)
            → 50 PRD threads start working
            → 200 tasks queue (Issue 2)
            → 50+ Bedrock calls fire (Issue 3)
            → 100+ cursors demanded (Issue 1)
            → Cascade failure
```

### Code Evidence

**Temporary 250-worker pool** (`leviathan_job.py` lines 1202-1204):
```python
with ThreadPoolExecutor(
    max_workers=max_workers,  # up to 250
    thread_name_prefix="leviathan-fanout",
) as pool:
    futures = [pool.submit(_invoke_one, rid) for rid in record_ids]
```

### Why It's a Bomb

The Lambda extraction takes 5-15 minutes. All 250 jobs were submitted simultaneously, so their webhooks arrive in a narrow time window. The PRD pool (50 threads) and Bedrock semaphore (5 slots) aren't designed for this burst pattern.

---

## Issue 5: Bus Notification Storm (Minor)

### What It Is

Every job state change emits a `bus.bus` notification to all connected browser sessions. At high throughput (50+ completions per minute), this floods connected browsers with reload triggers.

### Impact

- 5 `_sendone` call sites across the codebase (4 in `leviathan_job.py`, 1 in `controllers/main.py`)
- Each notification triggers a list view reload in every connected tasker's browser
- 20 taskers x 50 notifications/min = 1,000 browser refreshes per minute
- UI becomes sluggish but not broken

### Code Evidence

All notifications go through `_write_with_cursor` on state changes (`leviathan_job.py` line 2541-2549):
```python
if "state" in vals:
    env["bus.bus"]._sendone(
        "leviathan_job_updates",
        "leviathan/job_state",
        {"id": record_id, "state": vals["state"]},
    )
```

---

## The Cascade Pattern (How It Actually Crashes)

This is how all issues chain together during a batch run:

```
Step 1:  Admin fires batch of 250 jobs
         └─ 250 Lambda invocations (fast, no problem)

Step 2:  Over 5-15 min, ~250 webhooks arrive
         └─ Each webhook spawns a PRD generation thread

Step 3:  50 threads start, 200 queue (Issue 2: unbounded queue)
         └─ Memory grows, no backpressure

Step 4:  50 active threads each try to acquire Bedrock semaphore (5 slots)
         └─ 45 threads BLOCK on semaphore, holding cursor connections while waiting

Step 5:  HeartbeatTickers (50) compete for cursors every 60s (Issue 1)
         └─ 100 threads fighting for 64 cursors

Step 6:  Cursor pool exhausted
         └─ Odoo ORM operations block (form loads, list views, cron)
         └─ ALL modules freeze, not just Leviathan

Step 7:  12 Odoo workers x 5 Bedrock slots = 60 calls (Issue 3)
         └─ Blows past AWS quota → 429 throttling
         └─ Adaptive retry: calls take 5-30 min instead of 2-3s
         └─ Threads hold cursors even longer → worsens Step 6

Step 8:  Queued tasks wait hours
         └─ Watchdog retries extracting-state jobs (stale heartbeat)
         └─ More tasks enter the queue → feedback loop

Step 9:  Eventually: Odoo workers unresponsive → pod restarts
         └─ In-flight PRD generation lost
         └─ Jobs stuck in "generating" state with no worker
```

---

## Proposed Fixes

### Fix 1: Reduce Pool Size + Increase db_maxconn (Zero Code — Deploy Today)

**What**: Config-only change. Reduce thread pressure, increase cursor headroom.

**Change**:
```
LEVIATHAN_PRD_POOL_SIZE=15        # down from 50
db_maxconn=128                     # up from 64
```

At 15 PRD threads + 15 heartbeat threads = 30, well under 128 cursors.

**Trade-off**: Throughput drops (15 concurrent PRDs instead of 50) but stability improves. Buys time for proper fixes.

**Effort**: Zero code changes. Environment variable + Odoo config.

---

### Fix 2: Tune Bedrock Semaphore Per-Worker (Zero Code — Deploy Today)

**What**: Set `LEVIATHAN_BEDROCK_MAX_CONCURRENT` so total cluster-wide calls stay under AWS quota.

**Formula**: `max_concurrent_per_worker = floor(AWS_quota / total_workers)`

**Example** (quota=30 TPS, 12 workers):
```
LEVIATHAN_BEDROCK_MAX_CONCURRENT=2
# 2 x 12 = 24, safely under 30 TPS
```

**Trade-off**: Each worker processes fewer Bedrock calls in parallel but cluster stays under quota.

**Effort**: Zero code changes. Environment variable.

---

### Fix 3: Single Shared Heartbeat Thread (1 Day)

**What**: Replace 1-thread-per-job heartbeat with 1 shared thread for ALL jobs.

**Currently**:
```
50 active jobs → 50 HeartbeatTicker threads → 50 cursor acquisitions every 60s
```

**Proposed**:
```
50 active jobs → 1 HeartbeatManager thread → 1 cursor acquisition every 60s
(batch UPDATE ... WHERE id IN (...))
```

**Implementation**: Module-level `_HeartbeatManager` singleton with register/unregister. One thread wakes every 60s, collects all active job IDs, opens ONE cursor, does a single batch UPDATE, closes.

**Impact**: Eliminates ~50% of cursor pressure permanently.

---

### Fix 4: Bounded Queue with Admission Semaphore (Half Day)

**What**: Limit how many tasks can be queued + running. Excess tasks are deferred to the watchdog (which already handles this).

**Implementation**:
```python
_ADMISSION_LIMIT = _PRD_POOL_SIZE * 2  # 50 running + 50 queued max
_ADMISSION_SEMAPHORE = threading.Semaphore(_ADMISSION_LIMIT)

def _submit_bg(label, fn, *args, **kwargs):
    if not _ADMISSION_SEMAPHORE.acquire(timeout=0):
        _logger.warning("Pool full (%d), deferring '%s' to watchdog", _ADMISSION_LIMIT, label)
        return None  # Job stays in current state, watchdog retries later
    
    def _guarded():
        try:
            return fn(*args, **kwargs)
        finally:
            _ADMISSION_SEMAPHORE.release()
    
    pool.submit(_guarded)
```

**Impact**: Memory stays bounded. Watchdog naturally absorbs overflow — it already runs every 5 minutes and handles retries correctly.

---

### Fix 5: Staggered Batch Fanout (Half Day)

**What**: Instead of firing all 250 Lambda invocations at once, fire in waves of 30 with a delay between waves.

**Implementation**:
```python
WAVE_SIZE = 30
WAVE_DELAY_SECONDS = 60

for wave_start in range(0, len(record_ids), WAVE_SIZE):
    wave = record_ids[wave_start:wave_start + WAVE_SIZE]
    _fanout_batch_extraction(wave, ...)
    if wave_start + WAVE_SIZE < len(record_ids):
        time.sleep(WAVE_DELAY_SECONDS)
```

**Impact**: 250 jobs in waves of 30 = 9 waves over ~9 minutes. Webhooks arrive spread across 9-24 minutes instead of a 5-minute burst. PRD pool absorbs 30 per wave instead of 250 at once.

---

### Fix 6: PostgreSQL Advisory Lock for Bedrock (2-3 Days)

**What**: Replace per-process `threading.Semaphore` with a PostgreSQL advisory lock counter that coordinates across ALL workers and pods.

**Implementation**: Use `pg_advisory_lock` with unique lock IDs per Bedrock slot. All workers across all pods coordinate through the same database — no new infrastructure.

**Impact**: True cluster-wide Bedrock rate limiting. No Redis or external service needed.

---

### Fix 7: Dedicated Connection Pool (2-3 Days)

**What**: Create a separate `psycopg2.pool.ThreadedConnectionPool` for Leviathan background threads, isolated from Odoo's `db_maxconn`.

**Impact**: Complete isolation. Leviathan can never starve the rest of Odoo regardless of load. Properly sized pool with its own max connections.

---

### Fix 8: Debounced Bus Notifications (1 Day)

**What**: Buffer state-change notifications and emit them in batches every 5-10 seconds instead of per-job.

**Impact**: Frontend receives 1 message with 10 job IDs instead of 10 separate messages. Reduces browser reload storms.

---

## Implementation Priority

| Priority | Fix | Effort | Risk | Deploy |
|----------|-----|--------|------|--------|
| **P0** | Fix 1: Reduce pool size + raise db_maxconn | 0 code | None | Today |
| **P0** | Fix 2: Tune Bedrock semaphore per-worker | 0 code | None | Today |
| **P1** | Fix 3: Single shared heartbeat thread | 1 day | Low | This week |
| **P1** | Fix 4: Bounded queue + admission semaphore | 0.5 day | Low | This week |
| **P1** | Fix 5: Staggered batch fanout | 0.5 day | Low | This week |
| **P2** | Fix 6: PG advisory lock for Bedrock | 2-3 days | Medium | Next sprint |
| **P2** | Fix 7: Dedicated connection pool | 2-3 days | Medium | Next sprint |
| **P3** | Fix 8: Debounced bus notifications | 1 day | Low | Whenever |

**Recommended deployment order**:
1. **Today**: Fixes 1 + 2 (config only, zero risk, immediate stability)
2. **This week**: Fixes 3 + 4 + 5 (small code changes, address root causes)
3. **Next sprint**: Fixes 6 + 7 (proper architecture for 500+ concurrent scale)

---

## Appendix: Already Mitigated (Not Issues)

During analysis, two potential issues were investigated but found to be **already handled** in the code:

### Screenshot Double-Download (NOT an issue)

**Initially suspected**: QC re-downloads the same screenshots from S3 that PRD generation already downloaded.

**Actual code**: In the normal pipeline flow (`_run_prd_generation_bg_impl`), the `screenshot_blocks` variable is built once during PRD generation (line 2179-2218) and passed directly to QC (line 2426: `screenshot_blocks=screenshot_blocks`). **No re-download.** The standalone QC rerun (`_run_qc_only_bg`) does download from S3, but that's correct — it's a separate manual action.

### Watchdog False Positives (NOT an issue)

**Initially suspected**: The watchdog can't distinguish queued jobs from stuck jobs, causing false retries.

**Actual code** (`leviathan_job.py` lines 2725-2729):
```python
# `started_processing_at != False` excludes jobs sitting in the
# _POOL queue waiting for a worker — they look stuck (no heartbeat
# update) but no work has been attempted on them.
stale_generating = self.search([
    ("state", "in", ("generating", "scoring")),
    ("started_processing_at", "!=", False),  # <-- this guard
    ...
])
```

The developer already solved this with the `started_processing_at` field. For `extracting` state, Lambda sends a `started` heartbeat ping that serves the same purpose.
