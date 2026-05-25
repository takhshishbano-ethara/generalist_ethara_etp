# Vegeta

**Automated PRD generation pipeline for Ethara ETP.**

Give Vegeta a website URL and it produces a Product Requirements Document
(PRD) for that site — automatically. It looks at the live website, writes the
PRD with an AI model, scores the result, and hands it to a person for review.

---

## What it does, in one line

> **A website URL goes in → a scored PRD comes out.**

---

## The pipeline

Every piece of work is a **task** (one website). A task moves through these
stages from start to finish:

| Stage | What happens |
|-------|--------------|
| **Not Assigned** | Task exists but nobody owns it yet. |
| **Draft** | A tasker has the task and can start the pipeline. |
| **Extracting** | The website is crawled and analysed — pages, screenshots, tech stack, business signals and assets are collected. |
| **Generating PRD** | An AI model writes the PRD document from everything that was extracted, then runs a quality check on it. |
| **Scoring** | The finished PRD is graded against a fixed rubric, producing a score and a letter grade. |
| **Done** | The PRD is ready. The tasker reviews and edits it. |
| **Submitted** | The tasker is happy with the PRD and submits it. This is the finish line. |

Some tasks don't make it to the end:

| Stage | What happens |
|-------|--------------|
| **Failed** | Something went wrong (e.g. nothing could be extracted, or PRD generation broke). The task can be retried. |
| **Discarded** | The tasker decided the site isn't usable and stopped the work on purpose. |

---

## How a task flows

```
   Not Assigned
        │  tasker claims it
        ▼
      Draft  ──▶  Extracting  ──▶  Generating PRD  ──▶  Scoring  ──▶  Done
                                                                       │
                                                            tasker reviews & edits
                                                                       ▼
                                                                   Submitted
```

1. **Create a task** — enter the website URL.
2. **A tasker claims it** — taskers pick up the next available task, or an
   admin assigns one. Tasks can also be run in **batches** so many websites
   are processed at once.
3. **Extraction** — Vegeta analyses the live website and gathers the raw
   material: screenshots, the list of pages, the technology the site uses,
   business and authentication signals, and downloadable assets.
4. **PRD generation** — an AI model turns all of that raw material into a
   written PRD, and a second quality-check pass reviews it.
5. **Scoring** — the PRD is measured against a rubric and given a score and
   grade, so its quality is visible at a glance.
6. **Review** — the task reaches **Done**. A person reads the PRD, makes any
   edits, and then **Submits** it.

---

## The main pieces

| Piece | Role |
|-------|------|
| **Tasks** | The core record — one website and its PRD. Lives under the **Vegeta** menu in Odoo. |
| **Extraction service** | Visits the website and collects everything needed to write the PRD. |
| **AI PRD generation** | Writes the PRD document from the extracted data. |
| **Quality check (QC)** | A review pass that judges whether the PRD is shippable. |
| **Scoring** | Grades the finished PRD against a consistent rubric. |
| **Taskers & admins** | People who claim, review, edit and submit tasks. |
| **Background automation** | Scheduled jobs that keep work moving and recover tasks that get stuck. |

---

## Reliability

Vegeta is built so that work is never silently lost:

- Long-running steps run **outside** the main Odoo process, so they survive
  restarts.
- Scheduled background checks pick up unfinished work and retry or fail it
  cleanly, rather than leaving a task stuck forever.
- A task only fails when it genuinely cannot produce a PRD — a partial or
  imperfect extraction still counts as a success.

---

## Where to look next

- **`deploy/README.md`** — how PRD generation is deployed and run on
  Kubernetes (for DevOps).
- **`docs/RUNBOOK.md`** — step-by-step deployment / upgrade runbook.
- **`docs/EKS_DEPLOYMENT.md`** — deeper architecture notes.
- **`LOCAL_DEV.md`** — running Vegeta locally for development.
