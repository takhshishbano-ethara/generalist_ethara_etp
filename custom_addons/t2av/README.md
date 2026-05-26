# T2AV - Video Generation

T2AV is an Odoo 19 addon that generates videos from text prompts using the
OpenRouter Bytedance Seedance 2.0 endpoint. Submitted jobs are tracked through
a state machine, polled asynchronously in a background thread pool, and the
resulting MP4 is persisted to S3 via the existing `s3_connector` addon.

## Features

- Text-to-video generation via OpenRouter (`bytedance/seedance-2.0`)
- Async pipeline with live UI updates over `bus.bus`
- Fernet-encrypted API key storage (jaeger `credential_manager` pattern)
- Cost tracking (API-reported + local estimate via the Seedance token formula)
- Two-tier security: T2AV User (own records) and T2AV Manager (all + settings)
- Manual `Reconcile` action to recover records orphaned by a worker restart

## Dependencies

**Odoo modules:** `base`, `web`, `mail`, `bus`, `s3_connector`

**Python packages:** `cryptography`, `requests`

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `T2AV_ENCRYPTION_KEY` | Yes (prod) | Fernet key used to encrypt the OpenRouter API key at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. In dev, a key is auto-generated and persisted to `ir.config_parameter`. |
| `T2AV_ENCRYPTION_KEY_PREVIOUS` | No | Previous Fernet key used during a key rotation window. |
| `T2AV_POOL_SIZE` | No | Worker thread count for the pipeline executor (default 6). |

## Install

```bash
python src/odoo-bin -c odoo.conf -i t2av --stop-after-init
```

## Usage

1. As a T2AV Manager, open **Settings -> T2AV**, paste your OpenRouter
   API key, choose an S3 connector record, and save. Re-opening the page shows
   the masked sentinel `********`.
2. As a T2AV User, open **T2AV -> Generations -> New**, enter a prompt,
   pick duration / resolution / aspect ratio, and click **Generate**.
3. The status bar advances `queued -> processing -> downloading -> done`
   without manual refresh. The Video tab plays the MP4 once available.
4. If a worker restart leaves a record stuck, use the **Stuck (needs
   reconcile)** filter and click **Reconcile** on the record to resume.

## Testing

```bash
python src/odoo-bin -c odoo.conf --test-enable --test-tags t2av -u t2av --stop-after-init
ruff check custom_addons/t2av/
```
