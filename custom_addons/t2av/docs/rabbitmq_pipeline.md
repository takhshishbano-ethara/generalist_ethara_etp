# T2AV RabbitMQ Consolidated Pipeline

End-to-end automation of the t2av pipeline (enrich → golden prompt →
video submit → poll → download → S3 → presigned URL → done) driven by
RabbitMQ and a standalone `consumer.py` worker.

## Pipeline flow

```
CSV upload (existing wizard)  ─┐
                               │
List view multi-select  ───────┤
                               ├──► t2av.generation rows
Manual single record  ─────────┘     (state=draft, pipeline_status=not_published)
                                            │
                                            ▼
                       "Publish to RabbitMQ" header button
                                            │
                                            ▼
                       t2av.generation.action_batch_publish_pipeline
                       ├── validates prompt + category set
                       ├── sets pipeline_status = 'queued'
                       ├── env.cr.commit() (visible to consumer)
                       └── rabbitmq_service.batch_publish_pipeline_tasks
                                            │
                                            ▼
                                  ┌─────────────────────┐
                                  │  RabbitMQ           │
                                  │  t2av_pipeline      │
                                  │  └─ DLX → DLQ       │
                                  └──────────┬──────────┘
                                             │ prefetch=15
                                             ▼
                       consumer.py (1 process × 15 worker threads)
                                             │
                                             ▼
                       XML-RPC: t2av.generation.run_pipeline_sync(rid)
                       ├── SELECT FOR UPDATE + pipeline_status guard
                       ├── enrichment (sync, reuses existing client)
                       ├── _spawn_attempt + _run_submit_inline
                       ├── _pipeline_poll_until_terminal (in-thread)
                       ├── _run_download_inline (S3 + ir.attachment)
                       └── pipeline_status='done'
```

Throughput estimate: 200 records ≈ 50 min at 15 concurrent
(3.5 min/record average).

## Prerequisites

1. RabbitMQ broker reachable from both the Odoo host and the consumer host.
2. Odoo 19 installation with this module loaded.
3. Python deps on the consumer host: `pika`, `python-dotenv` (optional).
4. The `t2av_consumer_bot` user (auto-created on module install) with its
   password set by an admin via Settings → Users.

## Configuration

Copy `.env.example` to `.env` next to `consumer.py` and fill in:

| Variable | Required | Default | Notes |
|---|---|---|---|
| `RABBITMQ_HOST` | yes | — | No code fallback. |
| `RABBITMQ_PORT` | no | `5672` | |
| `RABBITMQ_USERNAME` | yes | — | |
| `RABBITMQ_PASSWORD` | yes | — | |
| `RABBITMQ_VHOST` | no | `/` | Recommended: dedicated `/t2av` vhost. |
| `RABBITMQ_QUEUE` | no | `t2av_pipeline` | |
| `RABBITMQ_DLX` | no | `t2av_pipeline.dlx` | |
| `RABBITMQ_DLQ` | no | `t2av_pipeline.dead` | |
| `ODOO_URL` | yes | — | e.g. `http://localhost:8069` |
| `ODOO_DB` | yes | — | |
| `ODOO_USERNAME` | yes | — | Use `t2av_consumer_bot`. |
| `ODOO_PASSWORD` | yes | — | |
| `CONSUMER_PROCESSES` | no | `1` | |
| `CONSUMER_WORKERS` | no | `15` | Hard cap on in-flight pipelines. |
| `CONSUMER_MAX_RETRIES` | no | `5` | After this, message → DLQ. |
| `CONSUMER_RETRY_BACKOFF` | no | `30` | Base seconds; exponential 2× growth. |
| `CONSUMER_RETRY_BACKOFF_CAP` | no | `600` | |
| `XMLRPC_TIMEOUT` | no | `2700` | Must exceed worst-case pipeline wall-clock. |

Odoo-side `ir.config_parameter` knobs (set via XML data file on install,
edit via Settings → Technical → System Parameters):

| Key | Default |
|---|---|
| `t2av.rabbitmq.queue_name` | `t2av_pipeline` |
| `t2av.rabbitmq.max_retries` | `5` |
| `t2av.pipeline.max_wall_clock_seconds` | `1800` |
| `t2av.pipeline.poll_initial_seconds` | `15` |
| `t2av.pipeline.poll_max_seconds` | `60` |
| `t2av.pipeline.watchdog_stale_seconds` | `900` |
| `t2av.pipeline.bedrock_concurrency` | `8` |

## Running the consumer

### One-shot test

```bash
cd custom_addons/t2av
python3 consumer.py
```

### Production supervisor

```bash
cd custom_addons/t2av
./run_consumers.sh
```

`CONSUMER_PROCESSES` controls how many competing consumer processes are
launched. With `CONSUMER_WORKERS=15` per process and `CONSUMER_PROCESSES=1`,
exactly 15 pipelines run concurrently.

The supervisor traps `SIGTERM` / `SIGINT` and gives each child up to
`T2AV_SHUTDOWN_GRACE` (default 30 s) to drain in-flight work before
escalating to `SIGKILL`.

### systemd unit (recommended)

```ini
[Unit]
Description=T2AV RabbitMQ consumer
After=network.target

[Service]
Type=simple
User=odoo
WorkingDirectory=/opt/ethara-etp/custom_addons/t2av
EnvironmentFile=/opt/ethara-etp/custom_addons/t2av/.env
ExecStart=/opt/ethara-etp/custom_addons/t2av/run_consumers.sh
KillSignal=SIGTERM
TimeoutStopSec=60
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Operations

### Publishing 200 records

1. Open *T2AV → Generations* (list view).
2. Filter to the records you want to publish.
3. Select all matching rows.
4. Click *Action → Publish to RabbitMQ* (or the header button).
5. Confirm. Records flip to `pipeline_status='queued'`.
6. Consumer picks them up at ~15 concurrent and they progress to
   `running` → `done`.

### Monitoring

- **Per-record**: list view `Pipeline Status` column (badge) + chatter log
  on each generation.
- **Aggregate**: consumer logs `STATS | completed=N permanent_fail=M
  transient_fail=K avg=...s` every 50 completions.
- **RabbitMQ depth**:
  ```bash
  rabbitmqctl list_queues name messages messages_unacknowledged
  ```
- **DLQ inspection**: any message in `t2av_pipeline.dead` warrants triage.

### Failure handling

| Failure type | Detection | Action |
|---|---|---|
| Network blip in worker | retryable exception | exp backoff republish, up to 5 attempts |
| OpenRouter 4xx | classified permanent | DLQ; pipeline_status='failed' on record |
| Validation error (prompt/category missing) | classified permanent | DLQ |
| Worker process crash | watchdog cron (15 min) | resets pipeline_status running → failed |
| Bedrock throttle | semaphore (8 concurrent) | natural backpressure inside worker |
| OpenRouter polling exceeds wall-clock | 30 min cap | permanent failure |

### Retrying a failed record

Open the record → click **Retry Pipeline** in the form header. This is
equivalent to publishing it fresh. Permanently-failed messages stay in
the DLQ for inspection; replaying them from the DLQ is a manual
operation via `rabbitmqctl shovel` or the management UI.

## Best-practice ledger (built in)

1. Idempotency: `SELECT FOR UPDATE` + `pipeline_status` guard, plus
   existing raw-SQL compare-and-set on completion in `t2av.attempt`.
2. Backpressure: AMQP `basic_qos(prefetch_count=CONSUMER_WORKERS)`.
3. Persistent messages: `delivery_mode=2`.
4. Durable queue + DLX/DLQ.
5. Retries with exponential backoff and gateway-error min-delay floor.
6. Permanent-failure short-circuit prevents zombie retries.
7. Reconnect-on-error in both publisher (`rabbitmq_service`) and
   consumer.
8. Chunked publish (50 messages per chunk with 100 ms gap) to avoid
   `Connection.Blocked` when publishing large batches.
9. Compare-and-set on completion remains the arbitrator if webhook is
   ever activated alongside the in-thread polling.
10. `bus.bus` live updates still fire for the OWL `live_status`
    component.
11. Structured logs with per-record-id traceability.
12. Secrets in `.env`, never in code (no hardcoded fallback credentials).
13. Watchdog cron resets stuck `running` records after the configured
    `t2av.pipeline.watchdog_stale_seconds`.
14. Graceful shutdown drains in-flight pipelines before exit.
15. Resource limits: Bedrock semaphore (configurable), OpenRouter
    per-call timeouts inherited from existing client.

## Troubleshooting

- **Consumer can't authenticate**: verify `t2av_consumer_bot` exists in
  Odoo and the `ODOO_PASSWORD` env value is correct.
- **Messages flow to DLQ immediately**: the consumer is treating
  exceptions as permanent. Check whether `run_pipeline_sync` is raising
  with a message containing one of the permanent-failure keywords
  ("permanent failure", "record does not exist", "missing required
  fields", etc.). Likely cause: `pipeline_status` advanced past
  `queued`/`failed` (idempotent no-op short-circuited).
- **Records stuck at `running`**: worker process crashed mid-pipeline.
  Wait for the watchdog cron (every 5 min) to reset them, then publish
  again.
- **OpenRouter rate-limited**: lower `CONSUMER_WORKERS`. The Bedrock
  semaphore already throttles enrichment to 8 concurrent.
- **Connection.Blocked from RabbitMQ**: broker low on disk. Increase
  `RABBITMQ_BATCH_CHUNK_DELAY` or scale broker storage.
