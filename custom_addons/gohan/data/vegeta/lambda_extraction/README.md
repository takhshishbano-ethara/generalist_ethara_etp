# Vegeta Extraction Lambda

Playwright-based website extraction service running on AWS Lambda. Receives extraction requests from the Odoo Vegeta module, runs a 9-phase pipeline to extract visual design, animations, assets, and architecture data from award-winning websites, then uploads artifacts to S3 and callbacks to Odoo.

## Architecture

```
Odoo (Vegeta Module)
  │
  ├─── POST /api/v1/extract ──→ Lambda Function URL (SigV4 auth)
  │                                  │
  │                                  ├── Phase 1: Site Discovery
  │                                  ├── Phase 2: Network Interception
  │                                  ├── Phase 3: Style + Brand + Dark Mode
  │                                  ├── Phase 4: Animations + WebGL + Auth + Audio
  │                                  ├── Phase 5: Asset Collection
  │                                  ├── Phase 6: Responsive Analysis
  │                                  ├── Phase 7: Wireframes
  │                                  ├── Phase 8: Performance + Codegen Export
  │                                  └── Phase 9: Build PRD Prompt
  │                                        │
  │                                        ├── Upload to S3 (screenshots + assets)
  │                                        │
  ◀── POST /webhook/extraction-complete ───┘
       (X-Vegeta-Token header)
```

## Deployment (ECR + Lambda)

### Prerequisites
- Docker
- AWS CLI configured with appropriate permissions
- ECR repository created

### Step 1: Build Docker Image
```bash
docker build -t vegeta-extraction .
```

### Step 2: Push to ECR
```bash
# Create ECR repo (one-time)
aws ecr create-repository --repository-name vegeta-extraction --region ap-south-1

# Login to ECR
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com

# Tag and push
docker tag vegeta-extraction:latest <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/vegeta-extraction:latest
docker push <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/vegeta-extraction:latest
```

### Step 3: Create/Update Lambda Function
```bash
# Create function (first time)
aws lambda create-function \
  --function-name vegeta-extraction \
  --package-type Image \
  --code ImageUri=<ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/vegeta-extraction:latest \
  --role arn:aws:iam::<ACCOUNT_ID>:role/vegeta-lambda-role \
  --timeout 900 \
  --memory-size 4096 \
  --architectures x86_64 \
  --environment "Variables={VEGETA_WEBHOOK_TOKEN=<TOKEN>,S3_BUCKET=production-grtlabs-tag,S3_REGION=us-east-1,PEXELS_API_KEY=<KEY>,PIXABAY_API_KEY=<KEY>,UNSPLASH_ACCESS_KEY=<KEY>}" \
  --region ap-south-1

# Update function code (subsequent deploys)
aws lambda update-function-code \
  --function-name vegeta-extraction \
  --image-uri <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/vegeta-extraction:latest \
  --region ap-south-1
```

### Step 4: Create Function URL
```bash
aws lambda create-function-url-config \
  --function-name vegeta-extraction \
  --auth-type AWS_IAM \
  --region ap-south-1
```

### Step 5: Grant Odoo's IAM User Permission to Invoke
```bash
aws lambda add-permission \
  --function-name vegeta-extraction \
  --statement-id allow-odoo-invoke \
  --action lambda:InvokeFunctionUrl \
  --principal <ODOO_IAM_USER_ARN> \
  --function-url-auth-type AWS_IAM \
  --region ap-south-1
```

### Alternative: SAM Deploy
```bash
sam build
sam deploy --guided \
  --parameter-overrides \
    VegetaWebhookToken=<TOKEN> \
    S3Bucket=production-grtlabs-tag \
    S3Region=us-east-1
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `VEGETA_WEBHOOK_TOKEN` | Yes | Shared secret sent as `X-Vegeta-Token` header in callbacks to Odoo. Must match `VEGETA_WEBHOOK_TOKEN` env var on Odoo server. |
| `S3_BUCKET` | Yes | S3 bucket for screenshots and asset storage (e.g., `production-grtlabs-tag`) |
| `S3_REGION` | No | AWS region for S3 operations (default: `us-east-1`) |
| `PEXELS_API_KEY` | Recommended | Pexels API key for copyright-free stock images in Page Assets. Get from https://www.pexels.com/api/ |
| `PIXABAY_API_KEY` | Recommended | Pixabay API key for copyright-free stock images. Get from https://pixabay.com/api/docs/ |
| `UNSPLASH_ACCESS_KEY` | Recommended | Unsplash Access Key for copyright-free stock images. Get from https://unsplash.com/developers |

> **Note:** At least one stock image API key is needed for Page Assets to include content images. Without any keys, Page Assets will contain only the generated text-logo SVG and safe decorative SVGs (fewer than 5 files). All extracted website images go to `_unused/` regardless, for copyright compliance.

## IAM Role Permissions

The Lambda execution role needs:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::production-grtlabs-tag/vegeta/*"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

## API Contract

### POST /api/v1/extract
**Auth:** AWS_IAM (SigV4 signature required)

**Request:**
```json
{
  "url": "https://example.com",
  "job_id": 42,
  "callback_url": "https://odoo.example.com/api/v1/vegeta/webhook/extraction-complete"
}
```

**Response (202):**
```json
{
  "success": true,
  "extraction_id": "ext-42",
  "message": "Extraction complete, callback sent"
}
```

### Callback Payload (POST to callback_url)
```json
{
  "job_id": 42,
  "success": true,
  "partial": false,
  "elapsed_seconds": 105.2,
  "site_discovery": {
    "title": "...",
    "description": "...",
    "url": "...",
    "category": "Normal Website",
    "tech_stack": {},
    "pages": []
  },
  "prd_prompt": "... (full extraction data formatted for PRD generation) ...",
  "artifacts": {"raw_data/site_discovery.json": "...", ...},
  "screenshot_keys": ["vegeta/42/screenshots/01_full_page.png", ...],
  "asset_keys": ["vegeta/42/assets/fonts/Inter.woff2", ...]
}
```

## Operational Notes

- **Timeout:** 900s hard limit (Lambda max). Internal deadline is 780s (13 min) to leave time for S3 upload + callback.
- **Memory:** 4096 MB (Playwright + Chromium requires ~2GB baseline).
- **Concurrency:** Limited to 10 concurrent executions to prevent cost explosion.
- **Graceful degradation:** If deadline is reached mid-extraction, partial results are sent with `"partial": true`.
- **Retry:** Callback has 3-attempt retry with exponential backoff. If all fail, results are lost (check CloudWatch).
- **Cold start:** ~15-20s (Docker image ~1.5GB with Chromium). Subsequent invocations are warm.

## File Structure
```
├── handler.py          # Lambda entry point + extraction orchestration
├── config.py           # Viewport sizes, breakpoints, phase config
├── modules/            # 20 extraction modules (one per concern)
│   ├── site_discoverer.py
│   ├── style_extractor.py
│   ├── animation_extractor.py
│   ├── asset_collector.py
│   ├── responsive_analyzer.py
│   ├── network_analyzer.py
│   ├── performance_analyzer.py
│   ├── brand_extractor.py
│   ├── component_token_extractor.py
│   ├── dark_mode_extractor.py
│   ├── auth_extractor.py
│   ├── interaction_capture.py
│   ├── webgl_extractor.py
│   ├── wireframe_generator.py
│   ├── codegen_exporter.py
│   ├── prd_writer.py           # Assembles extraction into PRD prompt
│   ├── stock_image_fetcher.py  # Pexels/Pixabay/Unsplash integration
│   ├── audio_extractor.py
│   ├── cursor_extractor.py
│   └── phase_gate.py           # Category-aware phase skipping
├── scripts/            # 14 JavaScript injection scripts
├── templates/          # Prompt templates
├── Dockerfile
├── requirements.txt
├── template.yaml       # SAM template (alternative deployment)
└── .dockerignore
```
