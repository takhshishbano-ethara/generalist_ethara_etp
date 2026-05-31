FROM ghcr.io/openclaw/openclaw:latest@sha256:95f4e860a1490e6e83f095d0bb1d2b5c7ac74e1cb62eedad08deb179e831a6de

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

ENV HOME=/home/node
# Pin brave plugin to a version compatible with the OpenClaw runtime above.
# Plugin 2026.5.28+ imports `readPositiveIntegerParam` from
# openclaw/plugin-sdk/provider-web-search, which is only exported by OpenClaw
# runtime 2026.5.28+. Our pinned runtime (digest 95f4e860..., reports version
# 2026.5.27) only exports `readNumberParam`. Mismatch produces the runtime
# error `(0 , _providerWebSearch.readPositiveIntegerParam) is not a function`.
# When bumping the OpenClaw image FROM line, bump this version in lockstep.
RUN node /app/openclaw.mjs plugins install @openclaw/brave-plugin@2026.5.27

# Amazon Bedrock provider plugin — required for memory_search embeddings.
# In OpenClaw <= 2026.5.12-beta.4 this was bundled in-core. From 2026.5.27+ it
# is a separately-published npm package (@openclaw/amazon-bedrock-provider).
# Without it, agents.defaults.memorySearch.provider="bedrock" throws
# `Unknown memory embedding provider: bedrock` from
# extensions/memory-core/src/memory/embeddings.ts:124. The provider id "bedrock"
# is contributed by plugin manifest id "amazon-bedrock"
# (extensions/amazon-bedrock/openclaw.plugin.json contracts.memoryEmbeddingProviders).
# Version must track the runtime version exactly (same constraint as brave).
RUN node /app/openclaw.mjs plugins install @openclaw/amazon-bedrock-provider@2026.5.27

# Symlink Playwright-installed Chromium to a standard path so OpenClaw can find it.
# OpenClaw scans /usr/bin/chromium, /usr/bin/chromium-browser, etc.
USER root
RUN CHROMIUM_BIN=$(find /home/node/.cache/ms-playwright -name "chrome" -type f 2>/dev/null | head -1) \
    && if [ -n "$CHROMIUM_BIN" ]; then \
         ln -sf "$CHROMIUM_BIN" /usr/bin/chromium; \
       fi

# Install gog CLI (Google OAuth helper)
ARG GOG_VERSION=0.12.0
RUN curl -fsSL "https://github.com/steipete/gogcli/releases/download/v${GOG_VERSION}/gogcli_${GOG_VERSION}_linux_amd64.tar.gz" \
    | tar -xz -C /usr/local/bin gog \
    && chmod +x /usr/local/bin/gog

USER node
WORKDIR /app

EXPOSE 18789

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD curl -f http://localhost:18789/healthz || exit 1
