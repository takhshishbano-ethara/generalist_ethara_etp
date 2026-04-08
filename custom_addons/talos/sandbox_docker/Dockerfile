FROM ghcr.io/openclaw/openclaw:latest@sha256:f38ff3d987e6c3e2d77bbb379de575f280d16037fd6a7ec334a35d48e480fd54

USER root

ARG DEBIAN_SNAPSHOT=20260406T000000Z
RUN echo "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm main" > /etc/apt/sources.list \
    && echo "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} bookworm-security main" >> /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/node/.openclaw/workspace/memory \
    /home/node/.openclaw/workspace/skills \
    /home/node/.cache/ms-playwright \
    && chown -R node:node /home/node/.openclaw /home/node/.cache

USER node
ENV PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright
RUN node /app/node_modules/playwright-core/cli.js install chromium

USER node
WORKDIR /app

EXPOSE 18789

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD curl -f http://localhost:18789/healthz || exit 1
