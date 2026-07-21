# ETP Assessment Pro

**Odoo 19 application** implementing the full **single-sitting** candidate
assessment lifecycle, driven by Vertex AI (Gemini for text/image, Veo for video).
An admin uploads a project SOP; Gemini authors draft questions from it natively;
approved questions form a bank; candidates sit a timed, proctored exam through
the website portal; objective answers are graded by code and subjective answers
by an LLM under an immutable-raw / live-threshold scoring model.

> **Manifest version:** 19.0.1.119.0 · **License:** LGPL-3 · **Application:** yes
>
> For the exhaustive, line-referenced internals see:
> - `docs/ETP_ASSESSMENT_FLOW.md` - complete feature & flow inventory (models, routes, crons, security, test matrix).
> - `docs/GENERATION_AND_SCORING_FLOW.md` - the LLM pipelines (generation, media render, scoring) prompt-by-prompt.
>
> Those two documents are the authoritative reference; this README is the orientation layer.

## What this module is

A self-contained assessment platform with four stages:

1. **AI question-bank generation (SOP-direct).** An admin creates a **Generator**
   (`etp.assessment.pro.prompt`), uploads the SOP / vendor / client documents as
   resources, and clicks *Generate from SOP*. The documents are sent **natively
   (base64 multimodal)** to Gemini, which authors draft questions directly - there
   is no intermediate skill-extraction step. Drafts are reviewed and approved into
   the **Question Bank** (`etp.assessment.pro.question`) or denied.
2. **Assessment setup & launch.** An admin creates an assessment bound to a
   generator, provisions candidates (CSV import → portal users), and launches:
   each candidate gets a shuffled question order, a tokenized exam URL, and a
   queued email invitation.
3. **Candidate exam (website portal).** Candidates sit the exam at
   `/pro_assessment/<token>` - a timed runner with client-side proctoring
   (tab-switch, copy-paste, right-click, devtools, screenshot, fullscreen,
   webcam, watermark).
4. **Scoring & results.** Objective questions are graded inline by code;
   subjective questions are graded by one batched Vertex call per candidate. The
   grader writes only an immutable raw score; pass/fail is derived live against a
   per-assessment threshold, so changing the threshold re-decides results without
   re-scoring.

## The seven question types

`constants.QUESTION_TYPE_SELECTION` is the single source of truth (seeded into the
`etp.assessment.pro.question.type` reference model at install).

| Type | Kind | Graded by |
|---|---|---|
| `mcq` | Objective - single choice | code, all-or-nothing |
| `msq` | Objective - multiple choice | code, all-or-nothing |
| `subjective_rubric` | Free-text (rubric supplied or model-generated) | LLM (v10 rubric) |
| `image_ab` | A/B image comparison (verdict axes + optional justification) | deterministic verdict, optional 0.75/0.25 LLM blend; flaw-injection by construction |
| `image_prompt` | Candidate writes the generating prompt for an image | LLM vs `ideal_prompt` |
| `image_label` | Candidate labels detected boxes on an image | LLM correctness × box coverage |
| `video_prompt` | Candidate writes the generating prompt for a video clip | LLM vs `ideal_prompt` (same lane as `image_prompt`) |

> **Removed types** (do not look for them): `subjective_justification` (folded into
> `subjective_rubric` with an empty rubric, migration 89.0) and `image_text`
> (split into `image_prompt` + `image_label`, migration 8.0).

## Key models

| Model | Purpose |
|---|---|
| `etp.assessment.pro` | Assessment container / single-sitting state machine |
| `etp.assessment.pro.evaluator` | One candidate's assignment/attempt (token, question order, scoring rollup) |
| `etp.assessment.pro.response` (+ `.response.line`) | Per-question answer (+ per-dimension line) |
| `etp.assessment.pro.question` | Published bank question (`generator_id`) |
| `etp.assessment.pro.question.dimension` (+ `.option`) | Self-contained per-question scoring axes |
| `etp.assessment.pro.question.image` / `.question.video` | Media for image/video questions (S3/CDN preferred) |
| `etp.assessment.pro.question.type` | Fixed 7-row generation vocabulary (seeded from constants) |
| `etp.assessment.pro.prompt` | **Generator** - SOP resources → drafts + tags + allow-list |
| `etp.assessment.pro.prompt.question` (+ `.dimension.option`) | Draft question awaiting approve/deny |
| `etp.assessment.pro.prompt.resource` | Uploaded source file + extracted text |
| `etp.assessment.pro.tag` | Semantic SOP tag (drives "Similar Projects" ranking) |
| `etp.assessment.pro.llm.usage` | Per-call token/cost ledger |
| `etp.assessment.pro.bank.import(.wizard)` | JSON/CSV question-bank importer |

`hr.applicant` and `res.config.settings` are inherited (candidate binding and the
configuration parameters, respectively).

## Configuration

**Settings → ETP Assessment** (Manager only) writes to `ir.config_parameter`
under the `etp_assessment_pro.*` namespace:

- **Vertex AI models** - `vertex_project_id`, `vertex_location`, and per-task
  model overrides: `generation_model` (reads the SOP; must stay document-capable),
  `vertex_model` / `image_model` (render), `scoring_model`, `detection_model`.
  Defaults live in `constants.py` (`gemini-3.1-pro-preview` for generation,
  `gemini-3-pro-image` for image/score).
- **Veo (video)** - `video_model` (default `veo-3.1-generate-001`),
  `video_location` (default `us-central1` - Veo 404s on the `global` location),
  `video_default_duration_s`.
- **Vertex auth** - one of: `vertex_api_key`, a static `vertex_access_token`, or an
  uploaded service-account JSON (`vertex_service_account_json`); with the SA JSON the
  module mints and refreshes 1-hour bearers itself (PyJWT + cryptography, RS256).
- **S3 storage** - `s3_bucket`, `s3_region`, `s3_access_key_id`, `s3_secret_key`,
  `s3_folder`, optional `s3_cdn_url`, `s3_max_retries`. Empty bucket disables S3 and
  the portal serves stored binaries directly.
- **Scoring** - `llm_max_attempts`, `scoring_batch_size` (default 8).
- **System prompts** - upload a custom `question.md` (`question_prompt`) or
  `scoring.md` (`scoring_system_prompt`) to override the bundled defaults; clearing
  the parameter reverts to the bundled file.

> Do **not** re-introduce a seed file for these parameters. `data/llm_config_parameters.xml`
> was retired in migration 117.0 precisely because seeded `noupdate` rows shadow
> the code defaults and freeze each setting at install - bumping a default in
> `constants.py` would then never reach a deployment.

## Automated flows (8 cron functions, 11 records)

All in `data/cron.xml`, `state=code`, each guarded by a unique Postgres advisory
lock, each committing before slow LLM calls:

| Cron | Interval | Does |
|---|---|---|
| `ir_cron_generate_from_sop` | 1 min | Drains ≤2 queued generators (native multimodal generation) |
| `ir_cron_render_pending_images` | 1 min | Renders ≤2 pending image drafts (all-or-nothing) |
| `ir_cron_detect_image_labels` | 1 min | Detects + annotates `image_label` images |
| `ir_cron_poll_video_ops` | 2 min | Submits + polls async Veo ops (`op_name` = double-bill guard) |
| `ir_cron_llm_auto_score` (x4: shards 0-3) | 1 min | Grades ≤20 submitted candidates/shard (one Vertex call each). Shard k drains `id %% scoring_shards == k`; set `scoring_shards` (1-4, default 1) to parallelize. |
| `ir_cron_recompute_subjective_results` | 1 min | Re-derives pass/fail after a threshold change |
| `ir_cron_send_pending_invitations` | 1 min | Sends ≤25 queued invitations |
| `ir_cron_expire_stale_attempts` | 1 min | Auto-submits ≤100 abandoned attempts past their deadline (deadline-rescue) |

> Tags extract **inline** from the *Extract Tags* button - there is no tag cron
> (removed in migration 98.0). The old `ir_cron_mark_missed` is gone too, but
> `ir_cron_expire_stale_attempts` is its single-sitting successor: a candidate who
> closes the tab past the deadline is auto-submitted by the sweep (or on their next
> request), so an abandoned attempt no longer pins the assessment open.

## Security

Two groups under the **ETP Assessment Pro** privilege
(`security/etp_assessment_security.xml`):

- **Evaluator** (`group_assessment_evaluator`) - read-only on the bank, tags, and
  dashboards; read+write on the working candidate rows (evaluator / response /
  response.line), narrowed by candidate-isolation record rules.
- **Manager** (`group_assessment_manager`) - full CRUD on every model + access to
  Configuration.

Candidates are `base.group_portal` users (or email-matched internal users). Their
only ACL is **read-only on question images and videos**
(`ir.model.access.csv`); every portal write goes through a controller `sudo()`
gated by the per-record `access_token`. Record rules key candidate isolation off
`*.candidate_user_id == user.id`.

## Dependencies

- **Odoo 19**, **Python 3.11+**.
- Odoo modules (`__manifest__.py`): `base`, `mail`, `hr`, `hr_recruitment`,
  `employee_extension`, `website`, `auth_signup`.
- Python (`external_dependencies`): `PyJWT` (service-account bearer minting -
  the crypto-enabled `PyJWT`, not the unrelated `jwt` package), `httpx` (Vertex /
  image / video transport), `boto3` (S3), `cryptography` (RS256 JWT signing).

## Tests

28 test modules under `tests/` (scoring v10, flaw-injection phases, image/video
prompt phases, portal HTTP, concurrency, integrity gates, DOM capture, tag
similarity, …) plus a **Locust load harness** (`tests/load/`) with a fake Vertex
backend for exercising 200–300 concurrent candidates. Run the unit suite with:

```bash
odoo -d <db> -i etp_assessment_pro --test-enable --stop-after-init
```
