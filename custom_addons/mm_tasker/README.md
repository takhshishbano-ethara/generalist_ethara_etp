# MM Tasker

**Authoring + QC + reference-model evaluation for the multimodal agentic eval pilot.**

MM Tasker is an Odoo 19 application that gives data taskers a structured workflow
to author an evaluation task (prompts + media + a `rubrics.jsonl` grading rubric),
gate each section through automated QC, dispatch the task across reference models
(Claude / GPT / Gemini), grade the responses against the rubric, and record a final
human QC verdict.

- **Module:** `mm_tasker`
- **Version:** `19.0.5.0.0`
- **Odoo:** 19.0
- **Depends:** `base`, `mail`
- **Author / License:** GRT Labs · LGPL-3

---

## What it does

A tasker builds one **Task** through a locked, gated pipeline:

1. **Prompts** — fill `default_prompt` + `human_prompt`; `final_prompt` is
   auto-composed (default + human, blank-line separated) and remains hand-editable.
2. **Media** — attach images / PDFs / videos (hard caps on count and total size).
3. **Rubrics** — upload `rubrics.jsonl`; each line is parsed into a child rubric row.
4. **Run Models** — dispatch the task across the active reference models; each model
   run is graded per-rubric.
5. **Run Judge** — an LLM judge grades the `response_criteria` / `response_not_criteria`
   rubrics that can't be graded deterministically.
6. **QC verdict** — a QC reviewer marks the task pass/fail (a fail reason is required).

Each authoring section (Prompts / Media / Rubrics) has its own **Submit** button that
runs a QC script as a subprocess. A **pass** verdict locks the section; a **fail**
leaves it editable with the failure message shown back to the tasker. All three
sections must pass before **Run Models** is allowed.

---

## Roles & access

Three security groups (Settings → Users), each with a record rule:

| Group | Sees | Notes |
|-------|------|-------|
| **Tasker** (`group_mm_tasker`) | Only their own tasks | Authors tasks; QC verdict is hidden from them |
| **QC** (`group_mm_qc`) | All tasks (read/write) | Marks the final pass/fail verdict |
| **Manager** (`group_mm_manager`) | All tasks (full) | Can unlock a task back to Draft (discards runs) |

Menus (under **MM Tasker**): *My Tasks*, *QC Queue*, *All Tasks*.

---

## Data model

| Model | Purpose |
|-------|---------|
| `mm.tasker.task` | The unit of work — prompts, media, rubrics, state, QC verdicts |
| `mm.tasker.rubric` | One rubric item parsed from a line of `rubrics.jsonl` |
| `mm.tasker.media` | An image / PDF / video asset (computed `kind`, `mime_type`, `file_size`) |
| `mm.tasker.run` | One reference-model execution of a task (`run_index` for N-per-model) |
| `mm.tasker.run.output` | Output file produced by a run (reserved for tool-use) |
| `mm.tasker.run.score` | Per-rubric verdict for a run (`passed` / `triggered` / `awarded_points`) |
| `mm.tasker.run.wizard` | "Run Models" dialog — pick runs-per-model, then dispatch |
| `mm.tasker.agent.dispatcher` | Abstract model — orchestrates dispatch + grading + the LLM judge |

### Task state machine

```
draft → ready_for_eval → dispatched → evaluated → qc_passed
                                                 ↘ qc_failed
```

Run state: `queued → running → judging → scored` (or `error`).
In live mode the run also carries an external `ext_state`
(`submitted → running → done`/`error`) tracked against the async service job.

---

## Rubrics format (`rubrics.jsonl`)

One JSON object per line. Recognised keys (extra keys are preserved in `raw_json`):

```json
{"number": 1, "type": "response_contains", "category": "accuracy", "points": 5, "importance": "mandatory", "criterion": "...", "needles": ["foo"]}
```

Supported rubric **types**:

- `probe_file_exists`, `probe_dir_exists`, `probe_file_contains`, `shell_succeeds_real`
  — graded by the backend against the agent's workspace (require live mode).
- `response_contains`, `response_regex_present` — graded locally in Python.
- `response_criteria`, `response_not_criteria` — graded by the **LLM judge** (Run Judge).

Uploads **upsert by `number`**, so re-uploading after dispatch updates rows in place
instead of deleting the existing `run.score` rows that depend on them.

### Scoring

Per run (matches the annotation guideline):

```
awarded   = Σ(points of passed positives) − Σ(|points| of triggered negatives)
max_total = Σ(points of positives)
per_task_score = clamp(awarded / max_total, 0..1)
passed    = all mandatory positives passed AND no mandatory negatives triggered
```

---

## Backend & test mode

The dispatcher routes on the `mm_tasker.test_mode` system parameter:

- **`true` (default)** — fully self-contained. Model runs and judge verdicts are
  **mocked** deterministically; no network calls, no backend required. Great for
  demos and exercising the whole workflow.
- **`false` (live)** — dispatches to an external "goku" service:
  - `POST /run` (async) → returns a `job_id`; the run is marked `running`.
  - A cron (`MM Tasker: Poll service jobs`, every minute) polls `GET /jobs/{id}`
    and ingests results when the job is `done`.
  - `POST /regrade` re-scores an existing run against new rubrics.
  - The LLM judge POSTs to `judge_backend_url` (falls back to `backend_url`).

The service grades workspace rubrics (`probe_*`, `shell_*`) and returns a `scores[]`
array; the backend's verdict wins whenever present. Rubric types that don't need a
workspace are graded locally.

---

## Configuration (System Parameters)

Set under **Settings → Technical → System Parameters**. Defaults are seeded by
`data/mm_tasker_config.xml`.

| Key | Default | Purpose |
|-----|---------|---------|
| `mm_tasker.test_mode` | `true` | Mock everything when truthy; go live when `false` |
| `mm_tasker.active_models` | `claude_opus_4_7,gpt_5_5,gemini_3_1` | Reference models to dispatch |
| `mm_tasker.model_labels` | `key:Label,...` | Pretty display names for model keys |
| `mm_tasker.backend_url` | *(empty)* | goku service base URL (live mode) |
| `mm_tasker.backend_token` | *(empty)* | Bearer token for the backend |
| `mm_tasker.backend_timeout` | `120` | Request timeout (s) for `/run` |
| `mm_tasker.judge_model_key` | `claude_opus_4_7` | Model used by the LLM judge |
| `mm_tasker.judge_backend_url` | *(empty)* | Judge endpoint (falls back to `backend_url`) |
| `mm_tasker.judge_cost_per_1m_in` / `_out` | `15.0` / `75.0` | Judge token cost (USD per 1M) for cost display |
| `mm_tasker.qc_timeout` | `30` | Subprocess timeout (s) for each QC script |
| `mm_tasker.media_max_file_mb` | `50` | Per-file size cap |
| `mm_tasker.media_max_count` | `60` | Max media files per task |
| `mm_tasker.media_max_total_mb` | `140` | Max total media (raw bytes) per task |

> Media caps are enforced as hard model constraints so no workflow path (UI or RPC)
> can exceed them; the total cap stays under the backend's request-body limit so
> dispatch fails early with a clear message instead of an opaque HTTP 413.

---

## QC scripts

The per-section Submit actions shell out to standalone scripts in `scripts/`, each
reading a JSON payload on **stdin** and writing `{"verdict": "pass"|"fail", "message": str}`
to **stdout**:

- `scripts/qc_prompt.py` — prompt-only checks (e.g. leftover `<PLACEHOLDER>` tokens).
- `scripts/qc_media.py` — media checks + cross-reference against the prompt.
- `scripts/qc_rubrics.py` — rubric checks + coherence with prompt and media.

A non-zero exit, timeout, bad JSON, or unknown verdict is treated as a **fail** with
the detail surfaced in the message — script issues never crash the workflow.

---

## Layout

```
mm_tasker/
├── __manifest__.py
├── data/
│   ├── mm_tasker_config.xml   # seeded system parameters
│   └── ir_cron.xml            # poll-jobs cron (live mode)
├── models/
│   ├── mm_tasker_task.py      # Task + per-section QC plumbing + state machine
│   ├── mm_tasker_rubric.py    # parsed rubric row
│   ├── mm_tasker_media.py     # media asset (kind/mime/size computes)
│   ├── mm_tasker_run.py       # run, run.output, run.score, poll cron
│   ├── mm_tasker_run_wizard.py# Run Models dialog
│   └── agent_dispatcher.py    # dispatch + grading + LLM judge
├── scripts/                   # qc_prompt.py / qc_media.py / qc_rubrics.py
├── security/                  # groups, record rules, ir.model.access.csv
├── views/                     # task / run / wizard / menus
└── migrations/                # 19.0.2 → 19.0.5
```

See `ARCHITECTURE.md` for the deeper design notes.

---

## Install

1. Ensure `mm_tasker` is on the Odoo addons path.
2. Update the apps list and install **MM Tasker** (or `-i mm_tasker` on the CLI).
3. Assign users to the **Tasker**, **QC**, or **Manager** group.
4. Leave `mm_tasker.test_mode = true` to try the full workflow with mocked models,
   or set it to `false` and configure `mm_tasker.backend_url` to run live.
