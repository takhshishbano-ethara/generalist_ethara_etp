# Preference Ranking Pipeline: S3 JSONL → QC Completed

## Architecture Overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│                           PRODUCTION ARCHITECTURE                           │
│                                                                             │
│  ┌─────────┐    ┌──────────────┐    ┌──────────────┐    ┌────────────────┐  │
│  │  S3     │───▶│ Odoo Server  │───▶│  RabbitMQ    │───▶│  Consumer(s)   │  │
│  │  JSONL  │    │  (HTTP API)  │    │  (Message Q) │    │  (Workers)     │  │
│  └─────────┘    └──────┬───────┘    └──────────────┘    └───────┬────────┘  │
│                        │                                        │           │
│                        │◄────────── XML-RPC ────────────────────┘           │
│                        │                                                    │
│                        ▼                                                    │
│                 ┌──────────────┐    ┌──────────────┐    ┌────────────────┐  │
│                 │  PostgreSQL  │    │  AWS Bedrock │    │  Meta GenAI    │  │
│                 │  (Database)  │    │  (Kimi LLM)  │    │  (Ophelia/     │  │
│                 └──────────────┘    └──────────────┘    │   Opalite)     │  │
│                                                         └────────────────┘  │
│                                     ┌──────────────┐    ┌────────────────┐  │
│                                     │   OpenAI     │    │  Google        │  │
│                                     │   (GPT 5.2)  │    │  (Gemini 3)    │  │
│                                     └──────────────┘    └────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Complete Pipeline Flow

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                    PHASE 1: INGESTION                                   │
 │                                                                         │
 │   S3 JSONL File                                                         │
 │       │                                                                 │
 │       ▼                                                                 │
 │   POST /api/get_jsonl_data                                              │
 │       │                                                                 │
 │       ├── Download JSONL from S3 URL                                    │
 │       ├── Parse JSON objects                                            │
 │       ├── Randomize A/B order (50% swap)                                │
 │       ├── Extract prompt from dialog_history                            │
 │       │                                                                 │
 │       ▼                                                                 │
 │   Create Records (chunks of 100)                                        │
 │       │                                                                 │
 │       ├── cr.commit() after each chunk                                  │
 │       │                                                                 │
 │       ▼                                                                 │
 │   Publish to RabbitMQ (chunks of 50)  ─────────────────────────┐        │
 │                                                                │        │
 └────────────────────────────────────────────────────────────────┼────────┘
                                                                  │
 ┌────────────────────────────────────────────────────────────────┼────────┐
 │                    PHASE 2: CONSUMER DISPATCH                  │        │
 │                                                                ▼        │
 │   RabbitMQ Queue: preference_ranking_eval                               │
 │       │                                                                 │
 │       ▼                                                                 │
 │   Consumer Process (4 processes × 5-15 threads)                         │
 │       │                                                                 │
 │       ├── Parse message: {"record_id": N, "action": "eval"}             │
 │       ├── Call Odoo via XML-RPC: eval_task([record_id])                 │
 │       │                                                                 │
 │       ├── On success → ACK message                                      │
 │       ├── On permanent failure → DROP message (no retry)                │
 │       └── On transient failure → Re-queue (max 3 attempts)              │
 │                                                                         │
 └─────────────────────────────────────────────────────────────────────────┘

 ┌─────────────────────────────────────────────────────────────────────────┐
 │                    PHASE 3: EVAL TASK (eval_task)                       │
 │                                                                         │
 │   ┌─────────────────────────────────────────────┐                       │
 │   │  Step 3a: Prompt Rejection Check            │                       │
 │   │                                             │                       │
 │   │  Call: prompt_rejection_check_sync_kimi()   │                       │
 │   │       │                                     │                       │
 │   │       ├── REJECTED → set is_processed=True, │                       │
 │   │       │              is_ratable=False, STOP │                       │
 │   │       │                                     │                       │
 │   │       └── ACCEPTED → continue ──────────────┼──┐                    │
 │   └─────────────────────────────────────────────┘  │                    │
 │                                                    │                    │
 │   ┌─────────────────────────────────────────────┐  │                    │
 │   │  Step 3b: Enhanced Prompt Generation        │◀─┘                    │
 │   │                                             │                       │
 │   │  Call: enhance_prompt_sync_kimi()           │                       │
 │   │  Sets: enhance_prompt field                 │                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                  │
 │   ┌──────────────────▼──────────────────────────┐                       │
 │   │  Step 3c: Response Generation (PARALLEL)    │                       │
 │   │                                             │                       │
 │   │  ThreadPoolExecutor(max_workers=2)          │                       │
 │   │       │                                     │                       │
 │   │       ├── Thread 1: GPT + Gemini responses  │                       │
 │   │       │   └─ OpenAI GPT 5.2                 │                       │
 │   │       │   └─ Google Gemini 3 Pro            │                       │
 │   │       │                                     │                       │
 │   │       └── Thread 2: Ophelia + Opalite       │                       │
 │   │           └─ Meta GenAI (model_1)           │                       │
 │   │           └─ Meta GenAI (model_2)           │                       │
 │   │                                             │                       │
 │   │  Sets: gpt_response, gemini_response,       │                       │
 │   │       ophelia_response_a, opalite_response_b│                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                  │
 │   ┌──────────────────▼──────────────────────────┐                       │
 │   │  Step 3d: Evaluation (5 PARALLEL TASKS)      │                       │
 │   │                                              │                       │
 │   │  ThreadPoolExecutor(max_workers=5)           │                       │
 │   │       │                                      │                       │
 │   │       ├── EVAL 1 (eval_ab):                  │                       │
 │   │       │   Response A vs Response B            │                       │
 │   │       │   → ab_preference, ab_comment         │                       │
 │   │       │   → gpt_preference, gemini_preference │                       │
 │   │       │   → 6 dimension scores per response   │                       │
 │   │       │                                      │                       │
 │   │       ├── EVAL 2 (eval_oph):                 │                       │
 │   │       │   Ophelia vs Opalite                  │                       │
 │   │       │   → enhance_ab_preference             │                       │
 │   │       │   → 6 dimension scores per response   │                       │
 │   │       │                                      │                       │
 │   │       ├── EVAL 3 (eval_gpt_sxs):             │                       │
 │   │       │   GPT vs Response B                   │                       │
 │   │       │   → gpts_ab_preference                │                       │
 │   │       │   → 6 dimension scores                │                       │
 │   │       │                                      │                       │
 │   │       ├── EVAL 4 (eval_gem_sxs):             │                       │
 │   │       │   Response A vs Gemini                │                       │
 │   │       │   → geminis_ab_preference             │                       │
 │   │       │   → 6 dimension scores                │                       │
 │   │       │                                      │                       │
 │   │       └── RUBRICS:                           │                       │
 │   │           Create rubrics + rate all models    │                       │
 │   │           → rubric1-4 name, description       │                       │
 │   │           → ophelia/opalite/gpt/gemini        │                       │
 │   │             rubric ratings                    │                       │
 │   │                                              │                       │
 │   │  All calls go to: AWS Bedrock (Kimi LLM)     │                       │
 │   │  Each eval internally makes 6 API calls:     │                       │
 │   │    Wave 1 (2 parallel):                      │                       │
 │   │      - Dimension scoring (all 6 dims)        │                       │
 │   │      - A/B comparison                        │                       │
 │   │    Wave 2 (4 parallel):                      │                       │
 │   │      - Compare vs Gemini                     │                       │
 │   │      - Compare vs GPT                        │                       │
 │   │      - Rubrics vs Gemini                     │                       │
 │   │      - Rubrics vs GPT                        │                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                   │
 │   ┌──────────────────▼──────────────────────────┐                       │
 │   │  Step 3e: Score Storage                      │                       │
 │   │                                              │                       │
 │   │  For each eval result:                       │                       │
 │   │    store_<dim>_<a/b>  (immutable LLM score)  │                       │
 │   │    <dim>_<a/b>        (editable human copy)  │                       │
 │   │    reason1_<dim>_<a/b> (AI reasoning text)   │                       │
 │   │                                              │                       │
 │   │  Dimensions: truthfulness,                   │                       │
 │   │    instruction_following, writing_quality,    │                       │
 │   │    verbosity, prompt_correctness,             │                       │
 │   │    overall_quality                            │                       │
 │   │                                              │                       │
 │   │  Comparisons: -3 to +3 scale                 │                       │
 │   │    (negative = A preferred,                   │                       │
 │   │     positive = B preferred)                   │                       │
 │   │                                              │                       │
 │   │  Sets: is_processed = True                   │                       │
 │   │        is_ratable = True                     │                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                   │
 └──────────────────────┼──────────────────────────────────────────────────┘
                        │
 ┌──────────────────────▼──────────────────────────────────────────────────┐
 │                    PHASE 4: QC CHECKS (run_qc_checks)                   │
 │                                                                         │
 │   Triggered: inline after eval_task() OR via RabbitMQ QC queue          │
 │                                                                         │
 │   ┌─────────────────────────────────────────────┐                       │
 │   │  Call: perform_qc_checks_sync_kimi()         │                       │
 │   │  (AWS Bedrock / Kimi LLM)                    │                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                   │
 │   ┌──────────────────▼──────────────────────────┐                       │
 │   │  QC Check 1: AI Detection                    │                       │
 │   │  - Scans comments & rubrics for AI-generated  │                       │
 │   │    phrases / hallucinations                   │                       │
 │   │  - Flags: reason1_ab_comment,                 │                       │
 │   │           reason1_gpt_comment, etc.           │                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                   │
 │   ┌──────────────────▼──────────────────────────┐                       │
 │   │  QC Check 2: Rubric-Comment Grounding         │                       │
 │   │  - Verifies rubric names/descriptions are     │                       │
 │   │    grounded in actual response content         │                       │
 │   │  - Checks rating consistency                  │                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                   │
 │   ┌──────────────────▼──────────────────────────┐                       │
 │   │  QC Check 3: Preference vs Comment Grounding  │                       │
 │   │  - Verifies ab_preference score matches       │                       │
 │   │    the ab_comment text                        │                       │
 │   │  - e.g., if score says A wins but comment     │                       │
 │   │    says B is better → FLAGGED                 │                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                   │
 │   ┌──────────────────▼──────────────────────────┐                       │
 │   │  QC Check 4: Rubric Rating Justification      │                       │
 │   │  - Verifies rubric ratings (1-6) are          │                       │
 │   │    justified by rubric descriptions           │                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                   │
 │   ┌──────────────────▼──────────────────────────┐                       │
 │   │  QC Check 5: External Preference Grounding    │                       │
 │   │  - Verifies GPT/Gemini preference scores      │                       │
 │   │    match their comparison comments            │                       │
 │   └──────────────────┬──────────────────────────┘                       │
 │                      │                                                   │
 │   ┌──────────────────▼──────────────────────────┐                       │
 │   │  QC Result                                    │                       │
 │   │                                              │                       │
 │   │  Sets: qc_task_status = 'pass' or 'fail'     │                       │
 │   │        reason1_* fields for any flagged items │                       │
 │   │        Token usage records created            │                       │
 │   └─────────────────────────────────────────────┘                       │
 │                                                                         │
 └─────────────────────────────────────────────────────────────────────────┘

 ┌─────────────────────────────────────────────────────────────────────────┐
 │                    PHASE 5: HUMAN EVALUATION                            │
 │                                                                         │
 │   Annotator opens record in Odoo form view                              │
 │       │                                                                 │
 │       ├── Reviews: prompt, responses, AI scores                         │
 │       ├── Adjusts: dimension scores (1-6)                               │
 │       ├── Adjusts: preference scores (-3 to +3)                         │
 │       ├── Reviews: QC flags (reason1_* tooltips)                        │
 │       │                                                                 │
 │       ▼                                                                 │
 │   Click "Evaluate" button → evaluate_task()                             │
 │       │                                                                 │
 │       ├── Compares human scores vs stored LLM scores                    │
 │       │   using check_error() (±1 tolerance)                            │
 │       │                                                                 │
 │       ├── Sets error_* flags where human disagrees                      │
 │       │   with AI by more than ±1                                       │
 │       │                                                                 │
 │       └── Sets: is_eval_done = True                                     │
 │                                                                         │
 │   ┌─────────────────────────────────────────────┐                       │
 │   │  Optional: Edit Enhanced Prompt               │                       │
 │   │                                              │                       │
 │   │  Click "Regenerate with New Prompt"           │                       │
 │   │  → action_submit_prompt()                    │                       │
 │   │       │                                      │                       │
 │   │       ├── Clears ALL downstream fields        │                       │
 │   │       ├── Resets: is_eval_done = False         │                       │
 │   │       │          is_processed = False          │                       │
 │   │       │          qc_task_status = False         │                       │
 │   │       └── Triggers: eval_task() again          │                       │
 │   │           (full re-evaluation)                │                       │
 │   └─────────────────────────────────────────────┘                       │
 │                                                                         │
 └─────────────────────────────────────────────────────────────────────────┘

 ┌─────────────────────────────────────────────────────────────────────────┐
 │                    PHASE 6: SUBMISSION                                   │
 │                                                                         │
 │   Click "Submit" → submit_task()                                        │
 │       │                                                                 │
 │       ├── Validates: is_eval_done == True                               │
 │       └── Sets: task_status = 'Submitted'                               │
 │                                                                         │
 │   ═══════════════════════════════════════                               │
 │   ║  PIPELINE COMPLETE                  ║                               │
 │   ═══════════════════════════════════════                               │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## Record Status Lifecycle

```
  CREATED          PROCESSED         QC DONE           EVALUATED          SUBMITTED
 ┌────────┐      ┌──────────┐      ┌─────────┐      ┌───────────┐      ┌──────────┐
 │        │      │          │      │         │      │           │      │          │
 │ task_id│─────▶│is_process│─────▶│qc_task_ │─────▶│is_eval_   │─────▶│task_     │
 │ prompt │      │ed = True │      │status = │      │done = True│      │status =  │
 │ resp_a │      │is_ratable│      │pass/fail│      │           │      │Submitted │
 │ resp_b │      │= True    │      │         │      │           │      │          │
 │        │      │          │      │         │      │           │      │          │
 └────────┘      └──────────┘      └─────────┘      └───────────┘      └──────────┘
     │                │                 │                  │                  │
     │           eval_task()      run_qc_checks()    evaluate_task()    submit_task()
     │                │                 │                  │                  │
   JSONL           Consumer          Consumer/         Human             Human
   Upload          (async)           Inline            Annotator         Annotator
```

---

## Data Flow Per Record

### Fields Set At Each Stage

| Stage | Fields Written | Source |
|-------|---------------|--------|
| **Ingestion** | `task_id`, `client_prompt`, `client_response_a`, `client_response_b`, `is_randomized` | S3 JSONL |
| **Prompt Rejection** | `prompt_rejection_reason`, `is_processed`, `is_ratable` | Kimi LLM |
| **Enhanced Prompt** | `enhance_prompt` | Kimi LLM |
| **Response Gen** | `gpt_response`, `gemini_response`, `ophelia_response_a`, `opalite_response_b` | OpenAI, Gemini, Meta GenAI |
| **Eval Scores** | `store_*_a/b`, `*_a/b`, `reason1_*_a/b` (6 dimensions × 4 eval types) | Kimi LLM |
| **Comparisons** | `ab_preference`, `ab_comment`, `gpt_preference`, `gemini_preference`, `enhance_ab_preference`, `gpts_ab_preference`, `geminis_ab_preference` + comments | Kimi LLM |
| **Rubrics** | `rubric1-4_name`, `rubric1-4_description`, `*_rubric1-4_rating` | Kimi LLM |
| **QC Checks** | `qc_task_status`, `reason1_*` (flagged items only) | Kimi LLM |
| **Human Eval** | Adjusted scores, `error_*` flags, `is_eval_done` | Human Annotator |
| **Submission** | `task_status = 'Submitted'` | Human Annotator |

---

## Concurrency Model

```
                    ┌──────────────────────────────┐
                    │     RabbitMQ Queues           │
                    │                              │
                    │  preference_ranking_eval ────┼──┐
                    │  preference_ranking_qc  ─────┼──┼──┐
                    └──────────────────────────────┘  │  │
                                                      │  │
    ┌─────────────────────────────────────────────────┼──┼────────┐
    │  Consumer Process 1                             │  │        │
    │  ThreadPoolExecutor(CONSUMER_WORKERS threads)   │  │        │
    │       │                                         │  │        │
    │       ├── Thread 1: eval record_id=101 ◀────────┘  │        │
    │       ├── Thread 2: eval record_id=102             │        │
    │       ├── Thread 3: qc  record_id=99  ◀────────────┘        │
    │       ├── ...                                               │
    │       └── Thread N                                          │
    └─────────────────────────────────────────────────────────────┘
    ┌─────────────────────────────────────────────────────────────┐
    │  Consumer Process 2 (same structure)                        │
    └─────────────────────────────────────────────────────────────┘
    ┌─────────────────────────────────────────────────────────────┐
    │  ...up to CONSUMER_PROCESSES                                │
    └─────────────────────────────────────────────────────────────┘

    Each eval_task() XML-RPC call triggers INSIDE Odoo:

    ┌─────────────────────────────────────────────────────────────┐
    │  Odoo Worker Process                                        │
    │                                                             │
    │  eval_task(record_id):                                      │
    │    ├── ThreadPool(2): response generation                   │
    │    │     ├── GPT + Gemini  ──▶ OpenAI / Gemini APIs         │
    │    │     └── Ophelia + Opalite ──▶ Meta GenAI API           │
    │    │                                                        │
    │    └── ThreadPool(5): evaluation                            │
    │          ├── eval_ab ──▶ Kimi/Bedrock (6 API calls)         │
    │          ├── eval_oph ──▶ Kimi/Bedrock (6 API calls)        │
    │          ├── eval_gpt_sxs ──▶ Kimi/Bedrock (6 API calls)   │
    │          ├── eval_gem_sxs ──▶ Kimi/Bedrock (6 API calls)   │
    │          └── rubrics ──▶ Kimi/Bedrock                       │
    │                                                             │
    │  Rate limiters (semaphores):                                │
    │    kimi:  max 10 concurrent                                 │
    │    genai: max 4 concurrent                                  │
    │    openai: max 5 concurrent                                 │
    │    gemini: max 5 concurrent                                 │
    └─────────────────────────────────────────────────────────────┘
```

---

## API Calls Per Record (Total)

| Phase | API Provider | Calls | Purpose |
|-------|-------------|-------|---------|
| Prompt Rejection | Kimi (Bedrock) | 1 | Check if prompt is valid |
| Enhanced Prompt | Kimi (Bedrock) | 1 | Generate enhanced version of prompt |
| Response Gen | OpenAI | 1 | GPT 5.2 response |
| Response Gen | Gemini | 1 | Gemini 3 Pro response |
| Response Gen | Meta GenAI | 2 | Ophelia + Opalite responses |
| Evaluation | Kimi (Bedrock) | 24 | 4 eval types × 6 calls each |
| Rubrics | Kimi (Bedrock) | 1 | Create + rate rubrics |
| QC Checks | Kimi (Bedrock) | 1 | All 5 QC checks |
| **Total** | | **~32** | **Per record** |

For 1000 JSONL records: **~32,000 API calls**

---

## Error Handling & Retry Strategy

```
  API Call Fails
       │
       ▼
  ┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
  │ Timeout?    │─Yes─▶│ Retry ×2     │─Fail─▶│ Skip this call,  │
  │ (120s)      │      │ with backoff │      │ field stays empty │
  └──────┬──────┘      └──────────────┘      └──────────────────┘
         │No
         ▼
  ┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
  │ HTTP 5xx?   │─Yes─▶│ Retry ×4     │─Fail─▶│ Raise exception  │
  │             │      │ with backoff │      │ → consumer retry │
  └──────┬──────┘      └──────────────┘      └──────────────────┘
         │No
         ▼
  ┌─────────────┐
  │ HTTP 4xx?   │──── Raise immediately (no retry)
  └──────┬──────┘
         │No
         ▼
     Success ✓


  Consumer Retry Logic:
  ┌──────────────────────────────────────────────────┐
  │  Message received from RabbitMQ                   │
  │       │                                           │
  │       ▼                                           │
  │  eval_task() via XML-RPC                          │
  │       │                                           │
  │       ├── Success → ACK, done                     │
  │       │                                           │
  │       ├── "Record does not exist" → DROP           │
  │       │   (permanent failure, no retry)            │
  │       │                                           │
  │       └── Other error → Re-queue                   │
  │           (max 3 attempts, 5s delay between)       │
  │           After 3 failures → PERMANENTLY FAILED    │
  └──────────────────────────────────────────────────┘
```

---

## Key Configuration

| Setting | Location | Default | Recommended (Production) |
|---------|----------|---------|------------------------|
| `workers` | odoo.conf | 0 | 4 |
| `limit_time_real` | odoo.conf | 300 | 600 |
| `db_maxconn` | odoo.conf | 32 | 64 |
| `CONSUMER_PROCESSES` | env / run_consumers.sh | 4 | 2 |
| `CONSUMER_WORKERS` | env / consumer.py | 15 | 5 |
| `KIMI_MAX_CONCURRENT` | env / rate_limiter.py | 10 | 10 |
| `GENAI_MAX_CONCURRENT` | env / rate_limiter.py | 4 | 4 |
| `OPENAI_MAX_CONCURRENT` | env / rate_limiter.py | 5 | 5 |
| `GEMINI_MAX_CONCURRENT` | env / rate_limiter.py | 5 | 5 |
