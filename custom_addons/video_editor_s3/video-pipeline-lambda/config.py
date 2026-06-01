import os

S3_BUCKET = os.environ.get("S3_BUCKET", "ethara-text-to-video")
S3_REGION = os.environ.get("S3_REGION", "ap-south-1")
WEBHOOK_TOKEN_SECRET_ARN = os.environ.get("WEBHOOK_TOKEN_SECRET_ARN", "")
YOUTUBE_COOKIES_SECRET_ARN = os.environ.get("YOUTUBE_COOKIES_SECRET_ARN", "")
CALLBACK_TIMEOUT_SECONDS = int(os.environ.get("CALLBACK_TIMEOUT_SECONDS", "15"))

YOUTUBE_TIERS = {
    "1080p": {"min_height": 1080, "max_height": 1080, "min_fps": 24, "max_fps": 60},
    "1440p": {"min_height": 1440, "max_height": 1440, "min_fps": 24, "max_fps": 60},
    "2160p": {"min_height": 2160, "max_height": 2160, "min_fps": 24, "max_fps": 60},
}

SUPPORTED_OPS = ("youtube_ingest", "render", "echo")
