# Cost-Fetch API Costs — Per Provider

This document records what each provider charges *us* to query their cost / billing data. These are the costs incurred by **this module's fetch operations**, not the costs of the services being tracked.

Numbers below reflect publicly documented pricing as of the implementation date. Verify against each provider's pricing page before relying on the totals for budgeting.

---

## TL;DR

| Provider | Endpoint | Pricing | Calls per fetch (current impl) | Per-fetch cost (USD) |
|----------|----------|---------|--------------------------------|----------------------|
| AWS | `Cost Explorer.get_cost_and_usage` | **$0.01 / request** | 2 (MONTHLY + DAILY) | **$0.02** |
| OpenRouter | `GET /api/v1/activity` | Free | 1 | $0.00 |
| OpenAI | `GET /v1/organization/costs` | Free (Admin API) | 1 | $0.00 |
| Moonshot | `GET /v1/users/me/balance` | Free | 1 | $0.00 |
| GCP | BigQuery query on Billing Export | **$5 / TiB scanned** (first 1 TiB/month free per billing account) | 1 (CTE with `UNION ALL` covers both granularities) | ~$0.00 in practice |

**Total per full multi-provider fetch (all providers enabled):** ~$0.02 (dominated by AWS).

---

## Provider details

### AWS — Cost Explorer

- **Endpoint:** `boto3.client('ce').get_cost_and_usage(...)`
- **Pricing:** **$0.01 per request.** Paginated requests are billed individually.
- **Free tier:** None for the Cost Explorer **API** (the AWS console is free, the API is not).
- **Current call count per fetch:** 2 — one MONTHLY call (window `start_month → end_month`), one DAILY call (window `start_month → today+1`).
- **Tracked in code:**
  - Constant: `AWS_CE_COST_PER_REQUEST_USD = 0.01` ([`aws_budget.py`](models/aws_budget.py))
  - Live counters: `api_hit_count`, `api_hit_cost_usd` on each `etp.project.aws.cost.fetch.log` row, surfaced in the **Fetch History** notebook page.
- **Pricing reference:** <https://aws.amazon.com/aws-cost-management/pricing/>

### OpenRouter — Activity API

- **Endpoint:** `GET https://openrouter.ai/api/v1/activity` (Management API key required, NOT a regular inference key).
- **Pricing:** **Free.** No per-request charge documented.
- **Rate limits:** Per-key throttling applies; no documented dollar cost.
- **Current call count per fetch:** 1.
- **Reference:** <https://openrouter.ai/docs>

### OpenAI — Organization Costs

- **Endpoint:** `GET https://api.openai.com/v1/organization/costs` (Admin API key `sk-admin-*` required; regular project keys are rejected).
- **Pricing:** **Free.** Admin endpoints are not billed per request.
- **Rate limits:** Organization-level rate limits apply; no documented dollar cost.
- **Current call count per fetch:** 1 (params: `bucket_width=1d`, `limit=200`, window = `fetch_months` back to start of current month).
- **Reference:** <https://platform.openai.com/docs/api-reference/usage>

### Moonshot — Balance Lookup

- **Endpoint:** `GET https://api.moonshot.ai/v1/users/me/balance`
- **Pricing:** **Free.** Balance lookup is not billable.
- **Note:** The endpoint returns only the *current* balance, not history. The implementation derives consumption via delta tracking against a stored baseline (`moonshot_last_used_usd`).
- **Current call count per fetch:** 1.
- **Reference:** <https://platform.moonshot.ai/docs/api-reference>

### GCP — BigQuery Billing Export

- **Mechanism:** SQL query against the standard Billing Export table (no GCP cost API exists). Setup steps live in [`GCP_SETUP_GUIDE.md`](GCP_SETUP_GUIDE.md).
- **Pricing:**
  - **On-demand query pricing:** $5 per TiB scanned.
  - **Free tier:** First **1 TiB per billing account per month** is free.
  - **Storage:** $0.02/GB-month for the export table (typically a few MB to a few GB).
- **Why it's effectively free in practice:** the implementation filters on partition-aligned `DATE(usage_start_time)` and only scans the last `fetch_months` of data. Typical scan per fetch: **< 100 MB**. Even at 100 fetches/day this stays well inside the free 1 TiB/month tier.
- **Current call count per fetch:** 1 BigQuery job (a single SQL statement with a CTE producing both daily and monthly granularities via `UNION ALL`).
- **Pricing reference:** <https://cloud.google.com/bigquery/pricing>

---

## How the module tracks API costs

Every fetch creates an `etp.project.aws.cost.fetch.log` row with:

- `api_hit_count` — number of underlying provider API calls made
- `api_hit_cost_usd` — accumulated dollar cost (currently AWS-only)

Today only the AWS path increments these counters because every other provider's API is free. If/when a provider starts billing for cost-data access, increment these in the corresponding `_fetch_<provider>_cost_one()` method.

---

## Cost estimation examples

Assume one budget with all providers enabled.

| Schedule | Fetches / month | Approx. monthly cost |
|----------|-----------------|----------------------|
| Manual only (1–2/day) | ~45 | **~$0.90** (AWS) |
| Hourly cron (24/day) | ~720 | **~$14.40** (AWS) |
| Every 6 hours (4/day) | ~120 | **~$2.40** (AWS) |

Non-AWS providers contribute $0 at any frequency. GCP only contributes if BigQuery scan totals exceed the 1 TiB/month free tier (extremely unlikely with this implementation).

**Recommendation:** keep the manual button + a low-frequency cron (every 6h is plenty for budget tracking — AWS itself only refreshes Cost Explorer data a few times a day, and Billing Export refreshes ~daily).

---

## Caveats

- **AWS CE API has no free tier.** Every call costs $0.01. Multiplying by number of budgets × fetches/day adds up.
- **AWS CE data freshness:** updated multiple times per day but not real-time. Sub-hourly fetches are wasteful — you'll pay $0.02/fetch for the same numbers.
- **GCP Billing Export freshness:** ~24h lag for standard export. Sub-daily fetches don't return newer data.
- **All upstream pricing/rate-limit policies can change.** Re-verify these numbers if you depend on the totals for finance reporting.
