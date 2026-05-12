# ARC Eval Launcher

Odoo 19 module that lets users launch ARC-Explainer eval runs from Odoo.

## What It Does (V1 / Ultra-MVP)

- **Launch wizard**: Select games, models, parameters -> POST to
  `arc-explainer` (`/api/eval/start`). All active models are pre-selected
  by default when the wizard opens. Games must be selected manually.
- **Session records**: Each launch creates an `arc.eval.session` row with
  the full request payload, the external session ID returned by
  arc-explainer, and the user who triggered it. Useful for audit and
  cross-referencing with arc-explainer's own DB.
- **Games / Models catalog**: Locally cached lists of available games and
  models, refreshed hourly from arc-explainer. Used to populate wizard
  dropdowns.

This is **trigger-only**: no live monitoring, no step streaming, no
dashboard. Sessions show "launched" or "failed to launch" based on the
HTTP response from arc-explainer. For live progress, use the arc-explainer
frontend directly.

## Installation

1. Copy this folder into your Odoo addons path (e.g.
   `custom_addons/arc_eval_launcher`).
2. Make sure Python `requests` is available in your Odoo virtualenv:
   ```
   pip install requests
   ```
3. In Odoo: **Apps** -> Update App List -> search "ARC Eval Launcher"
   -> Install.
4. Assign users to the **ARC Eval / User** or **ARC Eval / Administrator**
   group via *Settings -> Users*.

## Configuration

After install, configure via **Settings -> ARC Eval Launcher** (visible to
ARC Eval Administrators):

| Setting                | Default                  | Purpose                                           |
|------------------------|--------------------------|---------------------------------------------------|
| API Base URL           | `http://localhost:5000`  | Base URL of the arc-explainer server.             |
| Request Timeout (s)    | `30`                     | HTTP timeout (seconds) for API calls.             |
| Games Endpoint         | `/api/arc3/local-games`  | API path appended to base URL to fetch game list. |
| Game ID Field          | `game_id`                | JSON field holding the game identifier in response.|

These map to system parameters (`Settings -> Technical -> Parameters ->
System Parameters`) if you prefer editing them directly:

| Key                         | Default                  |
|-----------------------------|--------------------------|
| `arc_eval.api_base`         | `http://localhost:5000`  |
| `arc_eval.request_timeout`  | `30`                     |
| `arc_eval.games_endpoint`   | `/api/arc3/local-games`  |
| `arc_eval.game_id_field`    | `game_id`                |

### Switching Game List Endpoints

arc-explainer exposes three game list endpoints with different data sources:

| Endpoint               | ID Field   | Source                                  |
|------------------------|------------|-----------------------------------------|
| `/api/arc3/local-games`| `game_id`  | Local puzzle environment directories    |
| `/api/arc3/games`      | `game_id`  | Remote ARC3 competition API             |
| `/api/eval/games`      | `id`       | Static game IDs list in arc-explainer   |

Set **Games Endpoint** and **Game ID Field** accordingly. The response
shape is auto-detected (handles both `{ data: [...] }` and
`{ data: { games: [...] } }`).

## First Use

1. **Catalog -> Games** -> click **Actions -> Refresh from arc-explainer**
   (or wait up to 1 hour for the cron). Repeat for **Models**.
2. **Launch Eval** -> pick games + models + parameters -> click **Launch**.
3. A session record opens with the external session ID and the request /
   response payloads. If arc-explainer is unreachable or returns an error,
   the session is stored with `state=failed` and the error details.

## How It Talks to arc-explainer

All calls use the existing public HTTP API:

- `GET <games_endpoint>` -> populate game catalog (endpoint is configurable)
- `GET /api/eval/models` -> populate model catalog
- `POST /api/eval/start` -> trigger a run; returns `data.sessionId`

SSE streams (`GET /api/eval/stream/:id`) and run-detail endpoints are NOT
consumed in V1 - Odoo only triggers and records the launch. To watch a run
live, open `<api_base>/eval` in your browser.

## Wizard Parameters

| Field              | Default | Range  | Description                                   |
|--------------------|---------|--------|-----------------------------------------------|
| Games              | none    | -      | Which ARC3 games to evaluate (manual select)  |
| Models             | all     | -      | Which LLM models to evaluate                  |
| Runs per Model/Game| 1       | >= 1   | Number of runs per (game, model) pair         |
| Max Steps          | 200     | 1-200  | Maximum agent actions per run                 |
| Context Window     | 50      | >= 0   | Sliding window of recent turns in LLM prompt  |
| Include Grid Images| off     | -      | Send PNG grid images for vision models        |
| Parallel Games     | 1       | 1-25   | Games to run concurrently                     |
| Parallel Runs      | 1       | 1-10   | Runs per game to run concurrently             |
| Sequential Models  | off     | -      | Run models one after another                  |
| Global Budget (USD)| 0       | -      | Total spend cap (0 = unlimited)               |
| Per-Game Budget    | 0       | -      | Per-game spend cap (0 = unlimited)            |

## Security Model

- **ARC Eval / User**: can view games, models, and sessions; can launch evals.
  Users only see their own sessions.
- **ARC Eval / Administrator**: everything above + refresh catalog, manage
  sessions, edit cached records, see all sessions.

## Arc-Explainer Env Var Requirements

The eval orchestrator validates that selected models have their required
env vars set before starting. Model -> required env var mapping:

| Model Key              | Required Env Var   |
|------------------------|--------------------|
| `claude-opus`          | `CLOUD_API_KEY`    |
| `claude-opus-4.7`      | `CLOUD_API_KEY`    |
| `kimi-k2.5`           | `CLOUD_API_KEY`    |
| `gpt-5.4-thinking`    | `GPT_API_KEY`      |
| `gemini-3.1-standard` | `GEMINI_API_KEY`   |
| `gemini-3.1-priority` | `GEMINI_API_KEY`   |

Additionally, `CLAUDE_CLOUD_ARN`, `CLAUDE_47_CLOUD_ARN`, and
`KIMI_CLOUD_ARN` must be set (any truthy value) for the model registry
to load at startup.

## Upgrading After Code Changes

After modifying Python files or XML views, restart Odoo with:

```bash
python src/odoo-bin -d ethara_dev -c odoo.conf -u arc_eval_launcher
```

The cron and system parameters use `noupdate="1"`, so they won't be
overwritten on upgrade. Edit them manually via System Parameters.

## Roadmap (not in V1)

- V2: Poll `/api/eval/runs?sessionId=X` periodically (cron) to update
  session status beyond "launched".
- V3: SSE bridge via `queue_job` to stream live step events into Odoo
  mail.thread or a chart.
- V4: In-Odoo dashboard (port of `puzzle_monitor.py`).
- V5: Cancel support (`POST /api/eval/cancel/:id`).
