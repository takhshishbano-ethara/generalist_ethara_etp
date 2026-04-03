FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=0

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    libpq-dev \
    libxml2-dev \
    libxslt1-dev \
    libldap2-dev \
    libsasl2-dev \
    zlib1g-dev \
    libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ethara/app

COPY requirements.txt ./requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip setuptools wheel \
    && pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu \
    && grep -viE '^(torch|torchvision|torchaudio|nvidia-|triton)==' requirements.txt > requirements-filtered.txt \
    && pip install -r requirements-filtered.txt \
    && pip install "httpx[http2]"

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    gnupg \
    postgresql-client \
    libffi8 \
    libssl3 \
    libpq5 \
    libxml2 \
    libxslt1.1 \
    libldap-2.5-0 \
    libsasl2-2 \
    zlib1g \
    libjpeg62-turbo \
    libpng16-16 \
    libmagic1 \
    libglib2.0-0 \
    libgl1 \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libharfbuzz0b \
    libdatrie1 \
    libgraphite2-3 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    fontconfig \
    libfreetype6 \
    fonts-liberation \
    xfonts-75dpi \
    xfonts-base \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-eng \
    libreoffice-writer-nogui \
    libreoffice-calc-nogui \
    libreoffice-impress-nogui \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_amd64.deb && \
    apt-get update && \
    apt-get install -y --no-install-recommends ./wkhtmltox_0.12.6.1-3.bookworm_amd64.deb && \
    rm -f wkhtmltox_0.12.6.1-3.bookworm_amd64.deb && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g rtlcss

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /opt/ethara/app

COPY src/ ./src/
COPY custom_addons/ ./custom_addons/

RUN chmod +x src/odoo-bin \
    && mkdir -p /var/lib/odoo \
    && chmod 755 /var/lib/odoo

EXPOSE 8071 8100

CMD ["./src/odoo-bin", "-c", "odoo.conf"]
