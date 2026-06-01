# video-pipeline-lambda

AWS Lambda backend for `video_editor_s3` heavy lifting (YouTube ingest, ffmpeg trim/crop/scale).
Mirrors the layout and conventions of `custom_addons/leviathan/leviathan-extraction-lambda/`.

## Phase 1 status

This is an **echo stub**. The handler currently:

1. Accepts any payload with `op` ∈ `youtube_ingest | render | echo`.
2. Logs the payload to CloudWatch.
3. Posts an HMAC-signed callback back to `callback_url` with `{status: "echo", echo_payload, lambda_request_id}`.

No yt-dlp / ffmpeg work yet — that lands in Phase 2 (`modules/youtube_ingest.py`) and Phase 3 (`modules/render.py`).

## One-time AWS setup

```bash
# 1. Create the two secrets (one per env)
aws secretsmanager create-secret \
  --name ethara/video-pipeline/dev/webhook-token \
  --secret-string "$(openssl rand -hex 32)" \
  --region ap-south-1

aws secretsmanager create-secret \
  --name ethara/video-pipeline/dev/youtube-cookies \
  --secret-string file://./youtube_cookies.txt \
  --region ap-south-1
```

Capture the ARNs returned by each command — you'll pass them to SAM.

## Build + deploy

```bash
cd custom_addons/video_editor_s3/video-pipeline-lambda
sam build
sam deploy --guided   # first time only — answers stack name, region, parameters
# subsequent deploys:
sam deploy
```

`sam deploy --guided` will prompt for `WebhookTokenSecretArn` and `YoutubeCookiesSecretArn` — paste the ARNs from the secrets you created. SAM stores answers in `samconfig.toml`.

## Local invoke (no AWS account needed for unit testing)

```bash
sam build
sam local invoke VideoPipelineFunction --event events/echo.json
```

(Add `events/echo.json` with whatever payload shape you want.)

## Odoo-side wiring (Phase 2)

After deploy, set in Odoo Settings → Crowley Sourcing → Lambda:

| Param | Value |
|---|---|
| `video_editor_s3.lambda_function_name` | `video-pipeline-dev` (or `-stage` / `-prod`) |
| `video_editor_s3.lambda_region` | `ap-south-1` |
| `video_editor_s3.lambda_callback_base_url` | URL of the Odoo backend reachable from AWS (e.g. `https://ethara-stage.your-domain.com`) |

The Odoo controller `POST /video_editor_s3/callback/<op>` verifies the `X-Video-Pipeline-Token` HMAC against the same `WebhookTokenSecretArn` value, then writes back to the job / project.

## Operational notes

- **Rotating YouTube cookies**: `aws secretsmanager update-secret --secret-id ethara/video-pipeline/<env>/youtube-cookies --secret-string file://./youtube_cookies.txt`. No redeploy needed. Cold-start Lambdas pick up the new value.
- **Rotating webhook token**: same `update-secret` command on the webhook secret, AND update the matching value Odoo reads (Settings or `ir.config_parameter`).
- **Stuck job recovery**: a cron in Odoo (Phase 4) scrapes CloudWatch for `lambda_request_id` of jobs that exceeded a deadline and marks them failed with the actual log line.
