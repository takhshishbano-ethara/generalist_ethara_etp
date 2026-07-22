FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    ACCEPT_EULA=Y

RUN set -eux \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg \
        postgresql-client \
        build-essential gcc g++ \
        libffi-dev libssl-dev libpq-dev \
        libxml2-dev libxslt1-dev \
        libldap2-dev libsasl2-dev \
        zlib1g-dev libjpeg-dev \
        libmagic1 libglib2.0-0 libgl1 \
        libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 libharfbuzz0b libdatrie1 \
        libgraphite2-3 libx11-6 libxext6 libxrender1 \
        libjpeg62-turbo libpng16-16 \
        fontconfig libfreetype6 fonts-liberation \
        xfonts-75dpi xfonts-base \
        poppler-utils ffmpeg \
        tesseract-ocr tesseract-ocr-eng \
        libreoffice \
        nodejs npm \
        libnss3 libnspr4 \
        libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libxss1 \
        libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2 \
        libxkbcommon0 libwayland-client0 \
        libvulkan1 libgles2 libglx0 libglx-mesa0 \
        libdrm2 libdrm-amdgpu1 libdrm-intel1 libdrm-nouveau2 libdrm-radeon1 \
        default-jre-headless \
        unixodbc unixodbc-dev \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends msodbcsql18 msodbcsql17 \
    && mkdir -p /etc/apt/keyrings \
    && wget -qO /etc/apt/keyrings/githubcli-archive-keyring.gpg \
        https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && wget -q https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_amd64.deb \
    && apt-get install -y --no-install-recommends ./wkhtmltox_0.12.6.1-3.bookworm_amd64.deb \
    && rm wkhtmltox_0.12.6.1-3.bookworm_amd64.deb \
    && npm install -g rtlcss \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

RUN set -eux \
    && curl -fsSL https://github.com/steipete/gogcli/releases/download/v0.12.0/gogcli_0.12.0_linux_amd64.tar.gz \
        | tar -xz -C /usr/local/bin gog \
    && chmod +x /usr/local/bin/gog \
    && KUBECTL_VER=$(curl -fsSL https://dl.k8s.io/release/stable.txt) \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VER}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

WORKDIR /opt/ethara/app

COPY src/requirements.txt ./requirements.txt

RUN set -eux \
    && pip install --upgrade pip setuptools wheel \
    && pip install "Babel>=2.9.1" \
    && pip install -r requirements.txt \
    && pip install "httpx[http2]" \
    && playwright install chromium \
    && playwright install-deps chromium \
    && apt-get purge -y \
        build-essential gcc g++ \
        libffi-dev libssl-dev libpq-dev \
        libxml2-dev libxslt1-dev \
        libldap2-dev libsasl2-dev \
        zlib1g-dev libjpeg-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* \
    && find /usr/local/lib/python3.12 -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

RUN python -c "import language_tool_python; language_tool_python.LanguageTool('en-US').close()" \
    || echo "LanguageTool prefetch failed (non-fatal); jar will download on first use"

COPY src/ .
COPY . .
RUN chmod +x odoo-bin

EXPOSE 8071
CMD ["/bin/sh", "-c", "./odoo-bin -c odoo.conf -i erza,erza_dashboard,aurora_dashboard,milobench_dashboard -u erza,erza_dashboard,aurora_dashboard,milobench_dashboard --stop-after-init && exec ./odoo-bin -c odoo.conf"]
