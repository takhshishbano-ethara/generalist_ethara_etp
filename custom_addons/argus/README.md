# Argus

Video task management for AI-generated Instagram content review.

## What it is

A standalone Odoo 19 module that turns the QC backlog for AI video
generation into a tracked, filterable, reportable Odoo model.  Each
record (`argus.task`) carries:

* **Input Video URL** — the original Instagram reel that prompted the
  generation request.
* **Output Video URL** — the AI-generated rendition (also an
  Instagram reel).
* **Prompt** — the instruction sent to the generation model.
* **PL / QL** — Project Lead and Quality Lead, both `res.users`.
* **Email** — the task owner's contact address.
* **Task Status / QC Status / Final Decision** — three independent
  selections so a per-stage QC verdict doesn't pre-decide the final
  outcome.
* **QL Remarks / Tasker Remarks** — free-text notes per role.

## Workflow

```
                   ┌──────────┐
                   │ Pending  │
                   └────┬─────┘
                        │ Start
                        ▼
                ┌───────────────┐
                │ In Progress   │
                └──────┬────────┘
                       │ Submit to QC
                       ▼
   ┌─────────────────────────────────────────────┐
   │  QC Status:                                 │
   │  Pending → Approved / Rejected / Revision   │
   └─────┬───────────────┬─────────────┬────────┘
         │ approve       │ reject      │ revision
         ▼               ▼             ▼
    ┌────────┐      ┌─────────┐   ┌────────────────┐
    │Approved│      │Rejected │   │Needs Revision  │
    └────────┘      └─────────┘   └────────────────┘
```

Final Decision (Approved / Rejected / Pending) is a separate field
controlled by managers — it captures the outcome of any post-QC
sign-off and is the field that downstream reporting keys on.

## Key features

* Strict Instagram URL regex (reel / p / tv / reels) — invalid links
  raise on save.
* Email validation.
* Duplicate detection: a task is flagged when another active task
  carries the exact same (input shortcode, output shortcode) pair.
* PL / QL record rules: an Argus User sees only tasks they're the
  owner of or assigned to.  PLs / QLs see everything.
* Built-in pivot + graph dashboards for status mix and approval
  trends.
* Mail thread + activities — every status transition is logged
  in chatter.

## Permissions

| Group | Read | Write | Final Decision |
|---|---|---|---|
| Argus User | own + assigned | own | no |
| Project Lead | all | all in own scope | no |
| Quality Lead | all | QC fields | no |
| Manager | all | all | yes |

## Reports

* **Status Pivot** — `task_status × qc_status` count matrix.
* **Approval Trend** — line chart of `final_decision` per month.

Both are exportable to CSV / Excel via the standard Odoo export
button.

## Prompt QC with Kimi K2.5

The form has a **QC Prompt (Kimi K2.5)** button in the header that
sends the task's prompt to AWS Bedrock (Kimi K2.5 model) for
grammar / clarity grading. The result populates five columns:

* `prompt_grammar_score` — 0..100 (rendered as a progress bar)
* `prompt_grammar_level` — Poor / Fair / Good / Excellent (badge)
* `prompt_grammar_feedback` — one-paragraph verdict
* `prompt_grammar_response_json` — verbatim JSON for audit
* `prompt_grammar_checked_on` — timestamp

The QC verdict is auto-set in a single write that cascades to all
three status fields:

* `score >= argus.grammar_score_threshold` (**default 100** — strict mode)
  → `task_status` = `qc_status` = `final_decision` = `approved`
* `score < threshold`
  → all three flip to `rejected` + a red feedback banner with a
  **Re-run QC Prompt** button is shown inline.

By default Argus is in **strict mode**: only a perfect 100 / 100
score auto-approves. Lower the threshold under Settings → Argus →
Kimi K2.5 Configuration if you want a more lenient gate.

You can also override the **QC system prompt** sent to Kimi from
the same Settings page. The built-in default already produces the
JSON shape Argus needs; if you customise the prompt you must keep
that contract (`score` / `level` / `feedback` / `issues` /
`corrected_text` keys), otherwise response parsing fails.

## Inline video playback

The form's **Preview** icon (next to the Input / Output URLs) opens
a modal popup with the reel playing inline.  Under the hood this
fetches the direct CDN `.mp4` URL via
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and feeds it to a
native HTML5 `<video>` element — no redirect to instagram.com.

**One-time install** (inside the Odoo Python environment):

```bash
pip install yt-dlp
```

If `yt-dlp` is missing the popup falls back to regex scraping of
the public Instagram page, and finally to Instagram's own embed
iframe (which bounces on click).  A yellow banner inside the popup
tells you when you're in the fallback so it's never a mystery.

### Configuration

Argus calls Kimi K2.5 via AWS Bedrock's Converse API using its OWN
config keys (no shared config with `task_forge_core` — Argus is
standalone).

**Recommended path** — Settings → Argus → Kimi K2.5 Configuration
(visible to the Argus Manager group). The page has four fields:

| UI label | Backing parameter | Default |
|---|---|---|
| Bedrock API Key | `argus.kimi_api_key` | *(required)* |
| Bedrock Model ARN | `argus.kimi_model_arn` | *(required)* |
| AWS Region | `argus.kimi_aws_region` | `us-east-1` |
| Approve Threshold (0-100) | `argus.grammar_score_threshold` | `70` |

The same four params can also be set under **Settings → Technical →
Parameters → System Parameters** if you prefer raw key/value rows
(e.g. for scripted deploys via XML data files).

A chatter message is posted on every check with the score, level,
threshold, verdict, and the feedback text — the form's audit trail
shows every QC check that ran.
