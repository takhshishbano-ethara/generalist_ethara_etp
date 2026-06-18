# GCP Cost Fetch — Setup Guide

This module pulls GCP spend from your **BigQuery Billing Export**, the only Google-supported source of dollar-accurate cost data. The setup is a one-time GCP-side operation; after that every budget record that has GCP enabled will fetch alongside AWS.

---

## 1. Enable Billing Export to BigQuery

1. In the GCP Console, open **Billing** → **Billing export** → **BigQuery export**.
2. Click **Edit settings** under *Standard usage cost*.
3. Pick (or create) a project that will host the billing dataset — this can be any project you control. Note its **Project ID**.
4. Pick (or create) a dataset inside that project (e.g. `billing_export`). Note the **Dataset name**.
5. Save. GCP starts writing daily-partitioned tables into the dataset within ~24 hours. The table name has the form:

   ```
   gcp_billing_export_v1_<BILLING_ACCOUNT_ID_LAST_PART>
   ```

   Open the BigQuery console once the first table appears and copy its exact name.

> **Data freshness:** standard export refreshes roughly every 24h. There is no real-time billing API — this is as fast as Google publishes the data.

---

## 2. Create a service account with BigQuery read access

1. Open **IAM & Admin** → **Service Accounts** in the billing project.
2. Create a new service account, e.g. `etp-budget-reader`.
3. On the **Grant access** step add two roles (both required):
   - `roles/bigquery.dataViewer` — read the billing tables.
   - `roles/bigquery.jobUser` — run the SQL query.
4. Open the new service account → **Keys** → **Add key** → **Create new key** → **JSON**. Download the file.

> Keep the JSON safe — anyone with it can read your billing data.

---

## 3. Configure the budget record in Odoo

On the AWS Budget form, in the **GCP (BigQuery Billing Export)** section:

| Field | Value |
| --- | --- |
| **Fetch GCP Costs** | toggle on |
| **GCP Project ID** | the project from step 1 (e.g. `my-billing-project`) |
| **BigQuery Dataset** | dataset name from step 1 (e.g. `billing_export`) |
| **BigQuery Table** | exact table name from step 1, or a wildcard like `gcp_billing_export_v1_*` |
| **GCP Service Account JSON** | paste the full JSON key from step 2 |
| **GCP Service Filter** *(optional)* | exact `service.description` — e.g. `Generative Language API` for Gemini-only |
| **GCP Label Key / Value** *(optional)* | mirrors the AWS tag pattern; matches GCP resource labels |

Hit **Fetch GCP** in the header to test, or **Fetch Cost** to run the full multi-provider pipeline.

---

## 4. Gemini-only tracking

If you only care about Gemini spend (not the rest of GCP):

- Set **GCP Service Filter** to `Generative Language API`.
- Leave Label Key / Value empty.

The BigQuery query then sums only that one SKU and writes rows with the GCP service description as `service_name`.

---

## 5. Label-based scoping (mirrors AWS tags)

If you label GCP resources by team/project (e.g. `team=alpha`), set:

- **GCP Label Key** = `team`
- **GCP Label Value** = `alpha`

The query filters via `EXISTS (SELECT 1 FROM UNNEST(labels) AS l WHERE l.key = @label_key AND l.value = @label_value)`. Only labelled resources contribute to the total.

> **Caveat:** GCP services that don't accept labels (e.g. some networking SKUs) won't appear in label-scoped queries. Use Service Filter instead if you need an unlabelled product.

---

## 6. Cost & performance

- **Query cost:** BigQuery on-demand pricing is roughly **$5 per TB scanned**. The billing-export tables are small (typically a few hundred MB per month) so each fetch costs fractions of a cent. The first 1 TB / month is free under GCP's free tier.
- **Window:** the query covers the last `fetch_months` months by default (same field as AWS).
- **Both granularities:** each fetch returns **monthly** *and* **daily** rows in one SQL UNION, and the worker upserts both via `_upsert_provider_rows` (granularity column on `etp.project.aws.cost.line`).

---

## 7. Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Python packages 'google-cloud-bigquery' and 'google-auth' must be installed` | Run `pip install google-cloud-bigquery` on the Odoo server (google-auth is a transitive dep). |
| `Failed to load GCP credentials` | The pasted JSON is malformed or the account was deleted. Recreate the key. |
| `BigQuery query failed: 403 Access Denied` | Service account missing `bigquery.dataViewer` or `bigquery.jobUser` on the dataset. |
| `BigQuery query failed: 404 Not found: Table` | Table name typo, or Billing Export hasn't created the first table yet (wait ~24h after enabling). |
| `... contains unsupported characters` | Project ID / Dataset / Table fields only accept letters, digits, `_`, `-`, `.`, `*`. |
| No rows fetched | Either the Service Filter / Label Key/Value matches nothing, or the window predates Billing Export being enabled. |
