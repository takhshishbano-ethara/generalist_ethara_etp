# Crowley - Video Generation

Crowley is an Odoo 19 addon that generates videos from text prompts using the
OpenRouter Bytedance Seedance 2.0 endpoint. Submitted jobs are tracked through
a state machine, polled asynchronously in a background thread pool, and the
resulting MP4 is persisted to S3 via the existing `s3_connector` addon.

## Features

- Text-to-video generation via OpenRouter (`bytedance/seedance-2.0`)
- Async pipeline with live UI updates over `bus.bus`
- Fernet-encrypted API key storage (jaeger `credential_manager` pattern)
- Cost tracking (API-reported + local estimate via the Seedance token formula)
- Two-tier security: Crowley User (own records) and Crowley Manager (all + settings)
- Manual `Reconcile` action to recover records orphaned by a worker restart
- Duplicate-prompt prevention: a tasker cannot submit a generation whose prompt
  (case-insensitive, whitespace-collapsed) matches an attempt that is already
  in flight or completed on any job, system-wide. Managers can override
  per-record via **Allow Duplicate Prompt**.

## Duplicate-Prompt Prevention (v1.5.0)

The duplicate check fires from `_validate_can_submit`, so it blocks both the
initial **Generate** action and any subsequent **Retry**. It also surfaces as a
soft `onchange` warning while editing the prompt fields, so taskers see the
conflict before they click Generate.

**Matching rules**

- Normalization: trim, collapse internal whitespace to a single space,
  lowercase. Empty / whitespace-only prompts are not compared.
- Both `prompt` (Video Generation Prompt) and `original_prompt` are checked.
- Match scope: any `crowley.attempt` row with `state IN ('queued',
  'submitting', 'processing', 'downloading', 'done')` on any other
  `crowley.generation`. Cross-user and cross-company by design (uses `sudo()`
  to bypass record rules); the dataset must stay unique org-wide.
- Failed and cancelled attempts do not pollute the dup space (retry after
  real failure is allowed).

**Race-condition coverage**

If user A clicks **Generate** at T=0 (long pipeline, video lands at T=180s)
and user B submits the same prompt at T=60s while A is still in flight, B is
blocked: the in-flight states (`queued`, `submitting`, `processing`,
`downloading`) count as "this prompt is already taken." Without this, two
videos would be generated for the same prompt — wasted GPU cost and visibly
identical outputs.

**Manager override**

`crowley.generation.allow_duplicate` is a Boolean visible only to members of
**Crowley Manager** (field-level `groups=` ACL). Setting it to True bypasses
the application check on that generation. Use sparingly: legitimate reasons
include regenerating after a model upgrade, A/B testing, or replacing a
rejected video with a fresh take.

**Database constraint**

The post-migration script also creates two partial unique indexes:

```
crowley_attempt_dup_prompt_active_idx
    UNIQUE (prompt_normalized)
    WHERE state IN ('queued', 'submitting', 'processing', 'downloading', 'done')
      AND prompt_normalized IS NOT NULL

crowley_attempt_dup_original_prompt_active_idx
    UNIQUE (original_prompt_normalized)
    WHERE state IN ('queued', 'submitting', 'processing', 'downloading', 'done')
      AND original_prompt_normalized IS NOT NULL
```

These act as a defense-in-depth guard against TOCTOU races (two concurrent
submissions sneaking past the application check). On the indexed write the DB
raises `IntegrityError`, which Odoo surfaces as a transaction rollback.

**Migration runbook (19.0.1.4.0 -> 19.0.1.5.0)**

The post-migration script (`migrations/19.0.1.5.0/post-migration.py`) does
three things, in order:

1. Backfills `crowley_attempt.original_prompt` from each attempt's parent
   `crowley_generation.original_prompt`, so historical attempts participate
   in the dup space.
2. Populates `prompt_normalized` and `original_prompt_normalized` via SQL
   (same normalization as the Python code).
3. Audits the active rows (queued / submitting / processing / downloading /
   done) for pre-existing duplicates. If duplicates exist, the migration
   **logs a warning and skips index creation** — an administrator must clean
   up the duplicates manually before the constraint can be enforced. To
   re-run the index creation after cleanup, restart Odoo with `-u crowley`.

## Dependencies

**Odoo modules:** `base`, `web`, `mail`, `bus`, `s3_connector`

**Python packages:** `cryptography`, `requests`

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `CROWLEY_ENCRYPTION_KEY` | Yes (prod) | Fernet key used to encrypt the OpenRouter API key at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. In dev, a key is auto-generated and persisted to `ir.config_parameter`. |
| `CROWLEY_ENCRYPTION_KEY_PREVIOUS` | No | Previous Fernet key used during a key rotation window. |
| `CROWLEY_POOL_SIZE` | No | Worker thread count for the pipeline executor (default 6). |

## Install

```bash
python src/odoo-bin -c odoo.conf -i crowley --stop-after-init
```

## Usage

1. As a Crowley Manager, open **Settings -> Crowley**, paste your OpenRouter
   API key, choose an S3 connector record, and save. Re-opening the page shows
   the masked sentinel `********`.
2. As a Crowley User, open **Crowley -> Generations -> New**, enter a prompt,
   pick duration / resolution / aspect ratio, and click **Generate**.
3. The status bar advances `queued -> processing -> downloading -> done`
   without manual refresh. The Video tab plays the MP4 once available.
4. If a worker restart leaves a record stuck, use the **Stuck (needs
   reconcile)** filter and click **Reconcile** on the record to resume.

## Testing

```bash
python src/odoo-bin -c odoo.conf --test-enable --test-tags crowley -u crowley --stop-after-init
ruff check custom_addons/crowley/
```
