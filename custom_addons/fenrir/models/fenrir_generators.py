"""Generators for the auto-built task package.

At submit time, fenrir.task.action_submit_task() calls these helpers to
produce the structured outputs described in the requirements doc:

    <TASK_ID>/
    ├── task_metadata.json        ← _build_task_metadata
    ├── license.json             ← (existing _build_license_doc on the task)
    ├── environment/
    │   └── Dockerfile (+ setup.sh, nginx.conf)
    │                             ← _build_environment_files
    ├── tests/
    │   └── test_deliverables.sh|py
    │                             ← _build_validator_script
    └── submissions/seller_<n>/
        └── metadata.json         ← _build_seller_metadata
"""

import re

from ..lib import dockerfile_builder


def _runtime_variant_id(runtime_name):
    return re.sub(r"[^A-Za-z0-9]+", "_", runtime_name or "").strip("_") or "runtime"


SETUP_SH_TEMPLATE = """\
#!/bin/bash
set -e

# Environment setup for {code}: {title}
# Installs tools needed by tests/{test_filename}

apt-get update -qq
apt-get install -y --no-install-recommends \\
{package_block}

rm -rf /var/lib/apt/lists/*

echo "Environment ready. Run tests/{test_filename} <deliverables_dir> to validate."
"""


DOCKERFILE_STATIC_TEMPLATE = """\
FROM {base_image}

# Runtime deps for test validation and static serving
RUN apk add --no-cache \\
    bash \\
    coreutils \\
    file \\
    unzip \\
    curl \\
    && rm -rf /var/cache/apk/*

# Non-root user for nginx worker processes
RUN adduser -D -H -u 1001 -s /sbin/nologin appuser \\
    && chown -R appuser:appuser /var/cache/nginx \\
    && chown -R appuser:appuser /var/log/nginx \\
    && touch /var/run/nginx.pid \\
    && chown appuser:appuser /var/run/nginx.pid

COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY {test_filename} /opt/tests/{test_filename}
RUN chmod +x /opt/tests/{test_filename}

COPY setup.sh /setup.sh
RUN chmod +x /setup.sh

VOLUME ["{mount_path}"]

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=2 \\
    CMD curl -f http://localhost:80/ || exit 1

ENTRYPOINT ["/setup.sh"]
CMD ["serve"]
"""


ENTRYPOINT_STATIC_TEMPLATE = """\
#!/bin/bash
set -e

RUNTIME_DIR="{mount_path}"

case "$1" in
  test|validate)
    shift
    DELIVERABLES_DIR="${{1:-$RUNTIME_DIR}}"
    echo "Running validation tests against: $DELIVERABLES_DIR"
    exec /opt/tests/{test_filename} "$DELIVERABLES_DIR"
    ;;
  serve)
    if [ ! -f "$RUNTIME_DIR/index.html" ]; then
      echo "Warning: no index.html in $RUNTIME_DIR - mount your build to {mount_path}"
    fi
    echo "Serving on port 80..."
    exec nginx -g "daemon off;"
    ;;
  *)
    echo "Usage: docker run <image> [test|validate|serve]"
    echo "  test|validate [dir] - run unit tests against the deliverables directory"
    echo "  serve               - serve build on port 80"
    exit 1
    ;;
esac
"""


NGINX_CONF_TEMPLATE = """\
server {{
    listen 80;
    server_name localhost;
    root {mount_path};
    index index.html;

    location / {{
        try_files $uri $uri/ /index.html;
        add_header Cache-Control "no-cache, no-store, must-revalidate";
        add_header X-Content-Type-Options "nosniff";
        add_header X-Frame-Options "SAMEORIGIN";
    }}

    location ~* \\.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|mp3|ogg|wav)$ {{
        expires 1h;
        add_header Cache-Control "public, immutable";
    }}

    error_page 404 /index.html;

    location = /healthz {{
        access_log off;
        return 200 "ok\\n";
        add_header Content-Type text/plain;
    }}
}}
"""


# Default general-purpose image, used when NO runtime is selected on the task.
# Ubuntu base with broad, lightweight tooling so a reviewer can inspect /
# validate / run the common freelance deliverable types (documents, images,
# audio/video, archives, web + code projects). Heavyweight engines
# (Blender, Unity, Unreal, LibreOffice) are intentionally excluded — pick a
# dedicated runtime for those.
DOCKERFILE_UBUNTU_DEFAULT_TEMPLATE = """\
FROM ubuntu:24.04

# Default general-purpose sandbox (no runtime selected on the task).
# Broad tooling to inspect / validate / run typical freelance
# deliverables: documents (pdf, docx, pptx, md), raster + vector images
# (png, jpg, svg, ai/eps), audio + video (mp4, mov, wav), archives
# (zip, tar, 7z) and web / code projects (html, css, js, python, node).
ENV DEBIAN_FRONTEND=noninteractive \\
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
        bash coreutils ca-certificates curl wget git make jq \\
        unzip zip xz-utils p7zip-full unar \\
        file \\
        poppler-utils ghostscript libreoffice-nogui \\
        imagemagick librsvg2-bin \\
        ffmpeg mediainfo \\
        assimp-utils \\
        fonts-liberation fonts-dejavu-core fonts-noto-core fontconfig \\
        python3 python3-pip python3-venv \\
        nodejs npm \\
    && rm -rf /var/lib/apt/lists/*

# Non-root user (idempotent: skips if the base already defines one)
RUN getent passwd appuser >/dev/null \\
    || useradd --create-home --uid 1001 --shell /usr/bin/bash appuser

# Own the work/volume dir so the non-root user (and `shell` mode) can write
# to it. Applies to anonymous volumes; bind mounts keep host ownership — run
# with `--user $(id -u)` to match a host-owned deliverables directory.
RUN mkdir -p {mount_path} && chown appuser:appuser {mount_path}

WORKDIR {mount_path}

COPY {test_filename} /opt/tests/{test_filename}
RUN chmod +x /opt/tests/{test_filename}

COPY setup.sh /setup.sh
RUN chmod +x /setup.sh

VOLUME ["{mount_path}"]

# Optional HTTP preview port (used by the `serve` mode of setup.sh)
EXPOSE 8000

USER appuser

ENTRYPOINT ["/setup.sh"]
CMD ["test"]
"""


ENTRYPOINT_UBUNTU_TEMPLATE = """\
#!/bin/bash
# Deliberately NO `set -e`: dependency install and build are best-effort. A
# missing manifest or a failed optional step must NEVER crash the container -
# `serve` always degrades to a plain static file server (python3 is always
# present in this image), so the Dockerfile/container can't break.

RUNTIME_DIR="{mount_path}"
export PORT="${{PORT:-8000}}"

case "$1" in
  test|validate)
    shift
    DELIVERABLES_DIR="${{1:-$RUNTIME_DIR}}"
    echo "Running validation tests against: $DELIVERABLES_DIR"
    exec /opt/tests/{test_filename} "$DELIVERABLES_DIR"
    ;;
  serve)
    cd "$RUNTIME_DIR" 2>/dev/null || echo "[serve] Warning: $RUNTIME_DIR not present."
    if [ -f package.json ]; then
      echo "[serve] Node project detected - installing dependencies..."
      if [ -f yarn.lock ]; then yarn install --frozen-lockfile || yarn install
      elif [ -f pnpm-lock.yaml ]; then corepack enable 2>/dev/null; pnpm install || npm install
      elif [ -f package-lock.json ]; then npm ci || npm install
      else npm install
      fi
      if grep -q '"build"' package.json 2>/dev/null; then
        echo "[serve] Running build..."; npm run build || echo "[serve] build failed; serving source as-is."
      fi
      if grep -q '"start"' package.json 2>/dev/null; then
        echo "[serve] Starting via 'npm start' on port $PORT..."; exec npm start
      fi
      for d in dist build out public .next .; do
        if [ -d "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ]; then cd "$d"; break; fi
      done
      echo "[serve] Static-serving $(pwd) on 0.0.0.0:$PORT ..."; exec python3 -m http.server "$PORT"
    elif [ -f requirements.txt ] || [ -f pyproject.toml ] || [ -f setup.py ]; then
      echo "[serve] Python project detected - installing dependencies..."
      if [ -f requirements.txt ]; then pip3 install --no-cache-dir -r requirements.txt || true; fi
      if [ -f pyproject.toml ] || [ -f setup.py ]; then pip3 install --no-cache-dir . || true; fi
      if [ -f manage.py ]; then echo "[serve] Django detected"; exec python3 manage.py runserver "0.0.0.0:$PORT"
      elif [ -f app.py ]; then exec python3 app.py
      elif [ -f main.py ]; then exec python3 main.py
      else echo "[serve] No app entrypoint; static-serving $RUNTIME_DIR ..."; exec python3 -m http.server "$PORT"
      fi
    else
      if [ -f index.html ]; then echo "[serve] Static site (index.html) detected."
      else echo "[serve] No recognised manifest - static-serving the deliverables as-is."
      fi
      exec python3 -m http.server "$PORT"
    fi
    ;;
  shell|bash)
    cd "$RUNTIME_DIR" 2>/dev/null || true
    exec bash
    ;;
  *)
    echo "Usage: docker run <image> [test|validate|serve|shell]"
    echo "  test|validate [dir] - mechanical validation against the deliverables dir"
    echo "  serve               - auto-detect Node/Python/static, install+build+run; with"
    echo "                        no manifest it falls back to a static server (never errors)"
    echo "  shell               - open a bash shell in $RUNTIME_DIR"
    exit 1
    ;;
esac
"""


# Dev tasks that don't ship a webserver (Node SSR, Python API) — single-stage
# Dockerfile that just runs the test script. Annotators are expected to edit
# the serve mode to match their stack.
DOCKERFILE_RUNTIME_TEMPLATE = """\
FROM {base_image}

WORKDIR /srv/app

COPY {test_filename} /opt/tests/{test_filename}
RUN chmod +x /opt/tests/{test_filename}

COPY setup.sh /setup.sh
RUN chmod +x /setup.sh

VOLUME ["{mount_path}"]

EXPOSE 8000

ENTRYPOINT ["/setup.sh"]
CMD ["serve"]
"""


ENTRYPOINT_RUNTIME_TEMPLATE = """\
#!/bin/bash
set -e

RUNTIME_DIR="{mount_path}"

case "$1" in
  test)
    shift
    DELIVERABLES_DIR="${{1:-$RUNTIME_DIR}}"
    echo "Running validation tests against: $DELIVERABLES_DIR"
    exec /opt/tests/{test_filename} "$DELIVERABLES_DIR"
    ;;
  serve)
    echo "Serving from $RUNTIME_DIR..."
    cd "$RUNTIME_DIR"
    # TODO: replace with the correct serve command for this runtime
    #   Node:   exec node server.js
    #   Python: exec uvicorn main:app --host 0.0.0.0 --port 8000
    exec sleep infinity
    ;;
  *)
    echo "Usage: docker run <image> [test|serve]"
    exit 1
    ;;
esac
"""


VALIDATOR_SH_HEADER = """\
#!/bin/bash
# Auto-generated mechanical checks for {code}: {title}
# Verifies file existence only. Does NOT judge subjective quality.

DELIVERABLES_DIR="${{1:-.}}"

PASSED=0
FAILED=0

check_file() {{
  local rel="$1"
  if [ -f "$DELIVERABLES_DIR/$rel" ]; then
    echo "[PASS] $rel present"
    PASSED=$((PASSED + 1))
  else
    echo "[FAIL] $rel missing"
    FAILED=$((FAILED + 1))
  fi
}}

"""


VALIDATOR_SH_FOOTER = """
echo ""
echo "Results: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ] && exit 0 || exit 1
"""


VALIDATOR_PY_HEADER = """\
#!/usr/bin/env python3
\"\"\"Auto-generated mechanical checks for {code}: {title}.

Verifies file existence only. Does NOT judge subjective quality.
\"\"\"

import os
import sys

DELIVERABLES_DIR = sys.argv[1] if len(sys.argv) > 1 else "."

PASSED = 0
FAILED = 0


def check_file(rel):
    global PASSED, FAILED
    path = os.path.join(DELIVERABLES_DIR, rel)
    if os.path.isfile(path):
        print(f"[PASS] {{rel}} present")
        PASSED += 1
    else:
        print(f"[FAIL] {{rel}} missing")
        FAILED += 1


"""


VALIDATOR_PY_FOOTER = """

print()
print(f"Results: {PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
"""


# Maps the task-code prefix to the per-category env defaults.
_PREFIX_ENV_PROFILE = {
    "GD":  {"kind": "setup", "test_ext": "sh"},
    "3D":  {"kind": "setup", "test_ext": "py"},
    "GDV": {"kind": "static", "base": "nginx:1.25-alpine",
            "mount": "/srv/game", "test_ext": "sh"},
    "WD":  {"kind": "static", "base": "nginx:1.25-alpine",
            "mount": "/srv/app", "test_ext": "sh"},
    "SD":  {"kind": "runtime", "base": "python:3.12-alpine",
            "mount": "/srv/app", "test_ext": "sh"},
}


def _profile_for(code):
    prefix = (code or "").split("-", 1)[0]
    return _PREFIX_ENV_PROFILE.get(prefix, _PREFIX_ENV_PROFILE["GD"])


def test_filename_for(code):
    """Validator filename based on category. 3D uses Python, others Bash."""
    return f"test_deliverables.{_profile_for(code)['test_ext']}"


def _split_tags(raw):
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _split_packages(raw):
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def build_task_metadata(task):
    """Schema-compliant task_metadata.json per the requirements doc."""
    return {
        "task_id": task.code or "",
        "title": task.title or "",
        "category": task.category_id.name or "",
        "subcategory": task.subcategory or "",
        "price_bracket": task.price_tier or task.price_bracket or "",
        "recreation_notes": task.recreation_notes or "",
        "input_asset_licenses": [
            {
                "asset": lic.asset or "",
                "source": lic.source or "",
                "license": lic.license_label(),
                "url": lic.url or "",
            }
            for lic in task.input_asset_license_ids
        ] + [
            {
                "asset": att.file_name or "",
                "source": att.source_url or "",
                "license": att.license_label(),
                "url": att.source_url or "",
            }
            for att in task.attachment_ids
            if not att.is_generated and att.license and att.license != "self_created"
        ],
        "difficulty_estimate": task.difficulty_estimate or "",
        "estimated_completion_time_hours": task.estimated_completion_time_hours or 0,
        "tags": _split_tags(task.tags),
    }


def build_seller_metadata(offer):
    """Schema-compliant per-seller metadata.json."""
    return {
        "task_id": offer.task_id.code or "",
        "seller_number": offer.seller_no or 0,
        "seller_username": offer.seller_username or "",
        "seller_level": dict(offer._fields["seller_level"].selection).get(
            offer.seller_level, "") if offer.seller_level else "",
        "price_paid_usd": offer.price_paid_usd or 0,
        "order_date": offer.order_date.isoformat() if offer.order_date else "",
        "delivery_date": offer.delivery_date.isoformat() if offer.delivery_date else "",
        "delivery_time_days": offer.delivery_time_days or 0,
        "revisions_requested": offer.revisions_requested or 0,
        "order_id": offer.order_id or "",
        "seller_profile_url": offer.seller_profile_url or "",
    }


def build_environment_files(task):
    """Return list of (filename, str_content) for environment/ folder.

    Manual and Auto are mutually exclusive: if ANY of the four manual
    upload slots (dockerfile_attachment / dockerignore_attachment /
    nginx_conf_attachment / entrypoint_sh_attachment) is populated, the
    task is treated as Manual mode and this function returns an empty
    list — the export pipeline picks up the user-uploaded files via
    fenrir.task._environment_files() instead.

    Auto mode (no manual slots set) generates one Dockerfile per
    selected runtime via lib/dockerfile_builder.py. Single runtime →
    "Dockerfile"; multiple → "Dockerfile.<variant_id>" per runtime.
    When runtimes are selected, setup.sh + nginx.conf are emitted from
    the static templates. When NO runtime is selected, a general-purpose
    Ubuntu sandbox (DOCKERFILE_UBUNTU_DEFAULT_TEMPLATE + the Ubuntu
    setup.sh, no nginx.conf) is emitted instead, so submit-time
    generation always produces a usable environment.
    """
    manual_active = bool(
        task.dockerfile_attachment
        or task.dockerignore_attachment
        or task.nginx_conf_attachment
        or task.entrypoint_sh_attachment
    )
    if manual_active:
        return []

    test_filename = test_filename_for(task.code)
    mount_path = "/srv/app"

    files = []
    runtimes = task.environment_base_runtime_ids

    if runtimes:
        multi = len(runtimes) > 1
        needs_nginx = False
        for runtime in runtimes:
            vid = _runtime_variant_id(runtime.name)
            info = {
                "runtime": runtime.name,
                "desc": runtime.description or "",
                "deps": [d.name for d in runtime.key_dependency_ids if d.active],
            }
            content = dockerfile_builder.render(vid, info)
            filename = f"Dockerfile.{vid}" if multi else "Dockerfile"
            files.append((filename, content))
            if dockerfile_builder.profile_for(runtime.name).get("role") == "static-server":
                needs_nginx = True
        # Runtime-built images keep the static serve helper. nginx.conf is only
        # emitted when a static-server (nginx) runtime is selected — the
        # Dockerfile COPYs it only for that role, so other runtimes don't need it.
        files.append(("setup.sh", ENTRYPOINT_STATIC_TEMPLATE.format(
            mount_path=mount_path,
            test_filename=test_filename,
        )))
        if needs_nginx:
            files.append(("nginx.conf", NGINX_CONF_TEMPLATE.format(mount_path=mount_path)))
    else:
        # No runtime selected → general-purpose Ubuntu sandbox with broad
        # tooling for the common deliverable types. No nginx.conf — the
        # serve mode uses `python3 -m http.server` instead.
        files.append(("Dockerfile", DOCKERFILE_UBUNTU_DEFAULT_TEMPLATE.format(
            test_filename=test_filename,
            mount_path=mount_path,
        )))
        files.append(("setup.sh", ENTRYPOINT_UBUNTU_TEMPLATE.format(
            mount_path=mount_path,
            test_filename=test_filename,
        )))
    return files


def _parse_deliverable_lines(raw):
    """Pull filenames out of the free-form expected_deliverables text.

    Accepts one path per line. Strips bullets, leading dashes, and inline
    parenthetical notes — "logo.png (3000x3000)" yields "logo.png".
    """
    if not raw:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*•").strip()
        if not line:
            continue
        # Take the token before the first whitespace or '(' — handles
        # "logo.svg", "logo.png (3000x3000)", "editable .ai or .svg".
        token = re.split(r"[\s(]", line, 1)[0].strip()
        if token:
            out.append(token)
    return out


def _bash_escape(value):
    """Escape a single string for use inside double-quoted bash."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def _py_escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_validator_script(task):
    """Return (filename, str_content) — file-existence stub validator."""
    filename = test_filename_for(task.code)
    deliverables = _parse_deliverable_lines(task.expected_deliverables)

    if filename.endswith(".py"):
        header = VALIDATOR_PY_HEADER.format(
            code=task.code, title=task.title or task.code)
        if not deliverables:
            body = '# No expected deliverables declared.\nprint("[WARN] no expected deliverables")\n'
        else:
            body = "\n".join(
                f'check_file("{_py_escape(d)}")' for d in deliverables) + "\n"
        return filename, header + body + VALIDATOR_PY_FOOTER

    header = VALIDATOR_SH_HEADER.format(
        code=task.code, title=task.title or task.code)
    if not deliverables:
        body = '# No expected deliverables declared.\necho "[WARN] no expected deliverables"\n'
    else:
        body = "\n".join(f'check_file "{_bash_escape(d)}"' for d in deliverables) + "\n"
    return filename, header + body + VALIDATOR_SH_FOOTER
