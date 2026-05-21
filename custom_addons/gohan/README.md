# Gohan PRD Pipeline (Odoo 19 Addon)

Operator UI for the Gohan functional-PRD scraping pipeline. Lets analysts queue
website URLs, trigger Lambda-based extraction + LLM PRD generation, score the
output deterministically, and ship the QC-validated SOP deliverable.

This addon is the **operator front-end** for the pipeline whose offline
specification lives in `custom_addons/gohan/gohan/` (`HANDOFF_ODOO.md`,
`PLAN.md`, `MEMORY.md`, `CLAUDE.md`). The pipeline itself runs in AWS Lambda
and Bedrock; Odoo provides the data model, the run-trigger UI, the webhook
landing pad, and the deliverable browser.

## Architecture

Two parallel trigger paths are supported:

1. **Direct boto3 async invoke** (default, current production)
   - `gohan.job.action_run()` invokes the Lambda with
     `InvocationType=Event` via boto3.
   - Throughput controlled by `gohan.batch_concurrency` (default 250) and the
     Lambda's `ReservedConcurrentExecutions`.

2. **API Gateway HTTP POST** (spec path, optional)
   - `gohan.job.action_run_pipeline()` POSTs to `{gohan.lambda_api_url}/run`
     with `X-Api-Key: {gohan.lambda_api_key}`.
   - Use this when the pipeline is fronted by API Gateway rather than direct
     Lambda invoke.

Both paths converge on the same webhook callback contract — the pipeline
phones home with extraction artifacts and the run is finalized in Odoo.

## Webhook contracts

| Route | Auth | Used by |
|-------|------|---------|
| `POST /api/v1/gohan/webhook/extraction-complete` | `X-Gohan-Token` shared secret (env `GOHAN_WEBHOOK_TOKEN`) | Production extraction Lambda (artifacts + heartbeats) |
| `POST /gohan/webhook` | `X-Gohan-Signature` (hex HMAC-SHA256 of body, key = `gohan.hmac_secret`) | Spec-aligned API-Gateway flow (run-complete callback) |

### Spec webhook payload — `/gohan/webhook`

```json
{
  "run_id": 42,
  "status": "done",
  "score": 96.5,
  "qc_verdict": "shippable",
  "eq_tier": "API_DOCS",
  "s3_artifact_prefix": "s3://gohan-artifacts/runs/42/",
  "lambda_request_id": "abc-123-def",
  "error_message": null
}
```

`status` is one of `done` / `failed`. On `done`, fields are written to the
matching `gohan.job` record (the `run_id` is the `gohan.job.id`).
On `failed`, state flips to `failed` and `error_message` is stored.

## Models

- `gohan.category` — 16 seeded website categories with a slug `code`
- `gohan.job` — single record per pipeline run; tracks URL, state, score,
  QC verdict, EQ tier, mode, deliverable artifacts and S3 keys

The spec describes a 5-model split (`gohan.url` / `gohan.run` / `gohan.prd` /
`gohan.deliverable` / `gohan.category`). This addon collapses those into
`gohan.job` for operational simplicity while exposing every spec-mandated
field on that single model.

## System parameters

| Key | Purpose |
|-----|---------|
| `gohan.lambda_function_name` | AWS Lambda name/ARN for direct boto3 invoke |
| `gohan.lambda_region` | AWS region of the extraction Lambda |
| `gohan.extraction_access_key_id` / `gohan.extraction_secret_access_key` | AWS creds (leave blank → use pod IRSA) |
| `gohan.batch_concurrency` | Max parallel Lambda invocations per batch (default 250) |
| `gohan.bedrock_inference_arn` | Bedrock inference profile ARN |
| `gohan.bedrock_region` / `gohan.bedrock_access_key_id` / `gohan.bedrock_secret_access_key` | Bedrock AWS region + creds |
| `gohan.max_llm_attempts` | LLM retry budget per job |
| `gohan.s3_bucket` / `gohan.s3_region` / `gohan.s3_access_key_id` / `gohan.s3_secret_access_key` | Artifact bucket location + creds |
| `gohan.s3_folder` / `gohan.s3_cdn_url` | Optional S3 prefix + CDN |
| **`gohan.lambda_api_url`** | API Gateway base URL (spec path) |
| **`gohan.lambda_api_key`** | API Gateway `X-Api-Key` value |
| **`gohan.hmac_secret`** | Shared secret for `/gohan/webhook` HMAC-SHA256 verification |
| `gohan.prd_system_prompt` / `gohan.qc_system_prompt` | Uploaded prompt overrides |
| `gohan.max_jobs_per_user` | Active task quota per tasker |
| `gohan.watchdog_extracting_minutes` / `gohan.watchdog_generating_minutes` | Watchdog timeout thresholds |

All are managed under **Settings → Gohan**.

## Cron jobs

| Cron | Interval | Action |
|------|----------|--------|
| `Gohan: Watchdog Stuck Tasks` | 5 min | Recovers jobs stuck in `extracting`/`generating`/`scoring` past their heartbeat threshold |
| `Gohan: Reconcile Orphaned Runs` | 10 min | Spec-mandated: for jobs running >30 min, probe S3 for completion artifacts; mark `done` if found, `failed` otherwise |

## Security groups

- `group_gohan_user` — operators (create + run + view own tasks)
- `group_gohan_admin` — administrators (full control, all tasks, system parameters)

## S3 artifact layout

```
s3://{gohan.s3_bucket}/
└─ runs/{job_id}/
   ├─ raw_data/
   ├─ screenshots/
   ├─ assets/
   ├─ gohan_prd.md
   ├─ prd_data.json
   ├─ score_report.json
   ├─ QC_Report.md
   ├─ gohan_run.json
   └─ {site_name}_deliverables/
      ├─ prd.md
      ├─ website.md
      ├─ References/
      └─ Page Assets/
```

Suggested lifecycle: expire after 90 days.

## Dependencies

- **Odoo**: `base`, `base_setup`, `web`, `mail`, `bus`, `etp_user_roles`
- **Python**: `requests` (for API-Gateway path), `boto3` (for direct invoke,
  pulled in by the pipeline)

## Versioning

| Version | Notes |
|---------|-------|
| 19.0.2.0.0 | RabbitMQ removed; direct async Lambda invoke |
| 19.0.2.1.0 | PRD-as-deliverable; `discarded` state; LLM trace storage |
| 19.0.2.3.0 | Current production: thread-pool resilience + watchdog ping |
| 19.0.2.4.0 | Spec compliance overlay: API Gateway + HMAC webhook + spec fields + `code` field on `gohan.category` |
