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
  test)
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
    echo "Usage: docker run <image> [test|serve]"
    echo "  test [dir]  - run unit tests against deliverables directory"
    echo "  serve       - serve build on port 80"
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


def _parse_price_bracket(raw):
    """Best-effort parse of '$50-$100' or '$0-$50' style strings."""
    if not raw:
        return None
    m = re.match(r"\$?(\d+)\s*-\s*\$?(\d+)", raw.strip())
    if not m:
        return None
    return {"low": int(m.group(1)), "high": int(m.group(2))}


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
    setup.sh + nginx.conf are emitted from the static templates.
    Falls back to the legacy static Dockerfile when no runtimes are
    picked so submit-time generation never fails.
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
    else:
        base_image = task.environment_base_runtime or "nginx:1.25-alpine"
        files.append(("Dockerfile", DOCKERFILE_STATIC_TEMPLATE.format(
            base_image=base_image,
            test_filename=test_filename,
            mount_path=mount_path,
        )))

    files.append(("setup.sh", ENTRYPOINT_STATIC_TEMPLATE.format(
        mount_path=mount_path,
        test_filename=test_filename,
    )))
    files.append(("nginx.conf", NGINX_CONF_TEMPLATE.format(mount_path=mount_path)))
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
