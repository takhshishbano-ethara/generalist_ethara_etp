"""Dockerfile builder ported from scripts/generate_dockerfiles_2.py.

Pure-Python library (no Odoo dependencies) so it can be imported from
models/fenrir_generators.py at task-submit time AND run from the CLI
script unchanged. Only the CLI / CSV-ingestion parts of the source
script are stripped out; the classifier sets, PROFILES dict and the
render() function are byte-for-byte ports.

Keep the sets / PROFILES below in sync with the CLI script. If they
drift, the Odoo export and the standalone script will produce
different Dockerfiles for the same input.
"""

PIP = {
    "pandas", "numpy", "scipy", "scikit-learn", "statsmodels", "prophet",
    "matplotlib", "plotly", "openpyxl", "streamlit",
    "trimesh", "numpy-stl", "pygltflib",
    "pyloudnorm", "soundfile", "librosa",
    "fastapi", "uvicorn", "requests", "pytest", "lxml",
    "opencv-python-headless", "scikit-image", "pillow",
    "torch", "torchvision", "nibabel", "pydicom",
    "rasterio", "geopandas", "shapely", "pyproj", "fiona",
    "transformers",
    "tableauhyperapi", "icalendar",
}
PIP_CPU_INDEX = {"torch", "torchvision"}

NPM = {"gltf-validator", "http-server"}

MANUAL_APT = {"docker-cli", "docker-compose-plugin"}

DROP = {"ffprobe"}

COMPOSER = {"composer"}

KNOWN_APT = {
    "bash", "git", "curl", "unzip", "file", "make", "gcc", "g++",
    "build-essential", "chromium", "npm", "python3", "postgresql-client",
    "redis-server", "redis-tools", "ffmpeg", "mediainfo", "imagemagick",
    "sox", "inkscape", "poppler-utils", "ghostscript", "libreoffice",
    "pandoc", "sqlite3", "xvfb", "mesa-utils", "default-mysql-client",
    "admesh", "assimp-utils", "openscad", "nginx", "php",
}
KNOWN_APT_PREFIXES = ("lib", "fonts-", "python3-", "tesseract-ocr")

WARN_PKGS = {"redis-server"}

VARIANT_NOTES = {
    "runtime_react": [
        "# Build flow (entrypoint): npm ci && npm run build, then serve the",
        "# production bundle (dist/ or exported .next) with http-server.",
    ],
    "runtime_powerbi": [
        "# Power BI Desktop is Windows-only; this image validates the data side:",
        "# source CSV/XLSX schema and measures re-computed via pandas/openpyxl.",
    ],
    "runtime_tableau": [
        "# Validates Tableau at the data layer: .hyper extracts via",
        "# tableauhyperapi; .twb/.twbx are XML/zip and parse directly.",
    ],
}


def classify(dep):
    if dep in DROP:
        return "drop"
    if dep in COMPOSER:
        return "composer"
    if dep.startswith("@") or dep in NPM:
        return "npm"
    if dep in MANUAL_APT:
        return "manual"
    if dep.startswith("python3-"):
        return "apt"
    if dep in PIP:
        return "pip"
    return "apt"


def is_unverified(dep):
    return not (dep in KNOWN_APT or dep.startswith(KNOWN_APT_PREFIXES))


PROFILES = {
    "nginx": {"role": "static-server", "preinstalled": {"nginx"}},
    "linuxserver/blender": {"role": "s6-service", "preinstalled": {"blender"}},
    "jrottenberg/ffmpeg": {"role": "worker", "preinstalled": {"ffmpeg"}},
    "node": {"role": "worker", "preinstalled": {"node", "npm"}},
    "python": {"role": "worker", "preinstalled": {"python3", "pip"}},
    "debian": {"role": "worker", "preinstalled": set()},
    "barichello/godot": {"role": "worker", "preinstalled": {"godot"}},
    "mcr.microsoft.com/dotnet/sdk": {"role": "worker", "preinstalled": {"dotnet-sdk"}},
    "php": {"role": "worker", "preinstalled": {"php"}, "php_exts": True},
    "kicad/kicad": {"role": "worker", "preinstalled": {"kicad-cli"},
                    "needs_root": True, "system_python": True},
    "ghcr.io/osgeo/gdal": {"role": "worker",
                           "preinstalled": {"gdal-bin", "python3-gdal", "python3"},
                           "system_python": True},
    "amrit3701/freecad-cli": {"role": "worker",
                              "preinstalled": {"freecad", "python3"},
                              "system_python": True},
    "thyrlian/android-sdk": {
        "role": "worker", "run_as_root": True, "system_python": True,
        "preinstalled": {"android-sdk", "sdkmanager", "adb", "java", "jdk"},
        "setup": [
            "# Accept any not-yet-accepted Android SDK licenses (no-op otherwise).",
            "RUN yes | sdkmanager --licenses >/dev/null || true",
            "# Builds use the project's own wrapper (entrypoint): ./gradlew assembleDebug",
        ]},
    "unityci/editor": {
        "role": "worker", "run_as_root": True, "system_python": True,
        "preinstalled": {"unity", "unity-editor"},
        "setup": [
            "# Unity needs a license at RUNTIME — never bake it into the image:",
            '#   docker run -e UNITY_LICENSE="$(cat Unity_v20XX.ulf)" ...',
            "# The image tag MUST match the project's ProjectSettings/ProjectVersion.txt.",
        ]},
    "mcr.microsoft.com/playwright": {
        "role": "worker", "system_python": True,
        "preinstalled": {"node", "npm", "playwright", "chromium",
                         "firefox", "webkit"},
        "setup": [
            "# Browsers are preinstalled under /ms-playwright. Keep this image tag",
            "# in sync with the project's @playwright/test version.",
        ]},
    "gradle": {"role": "worker", "preinstalled": {"gradle", "java", "jdk"}},
    "eclipse-temurin": {"role": "worker", "preinstalled": {"java", "jdk"}},
}
DEFAULT_FLAGS = {"system_python": False, "needs_root": False,
                 "php_exts": False, "run_as_root": False, "setup": []}


def repo_of(base):
    slash = base.rfind("/")
    colon = base.rfind(":")
    return base[:colon] if colon > slash else base


def profile_for(base):
    known = PROFILES.get(base)
    if known is None:
        known = PROFILES.get(repo_of(base))
    if known is None:
        return {"role": "worker", "preinstalled": set(), "system_python": True,
                "needs_root": False, "php_exts": False, "run_as_root": False,
                "setup": []}
    return {**{"role": "worker", "preinstalled": set()}, **DEFAULT_FLAGS, **known}


def detect_pkg_manager(base):
    return "apk" if "alpine" in base.lower() else "apt"


def multiline(cmd_head, pkgs, tail=""):
    body = " \\\n        ".join(pkgs)
    out = f"{cmd_head} \\\n        {body}"
    if tail:
        out += f" \\\n    {tail}"
    return out + "\n"


def apt_block(pkgs):
    return multiline("RUN apt-get update && apt-get install -y --no-install-recommends",
                     pkgs, "&& rm -rf /var/lib/apt/lists/*") if pkgs else ""


def apk_block(pkgs):
    return multiline("RUN apk add --no-cache", pkgs,
                     "&& rm -rf /var/cache/apk/*") if pkgs else ""


def pip_block(pkgs):
    return multiline("RUN pip3 install --no-cache-dir", pkgs) if pkgs else ""


def pip_cpu_block(pkgs):
    if not pkgs:
        return ""
    return ("# CPU wheels (~200MB vs multi-GB CUDA default). For GPU builds,\n"
            "# switch the index URL to e.g. https://download.pytorch.org/whl/cu121\n"
            + multiline("RUN pip3 install --no-cache-dir "
                        "--index-url https://download.pytorch.org/whl/cpu", pkgs))


def npm_block(pkgs):
    return multiline("RUN npm install -g", pkgs) if pkgs else ""


def user_block(pkg_mgr, role):
    if role == "s6-service":
        return ""
    if role == "static-server":
        return ("# Non-root user + nginx runtime dirs\n"
                "RUN adduser -D -H -u 1001 -s /sbin/nologin appuser \\\n"
                "    && chown -R appuser:appuser /var/cache/nginx /var/log/nginx \\\n"
                "    && touch /var/run/nginx.pid \\\n"
                "    && chown appuser:appuser /var/run/nginx.pid\n")
    if pkg_mgr == "apk":
        return "# Non-root user\nRUN adduser -D -H -u 1001 -s /sbin/nologin appuser\n"
    return ("# Non-root user (idempotent: skips if the base already defines one)\n"
            "RUN getent passwd appuser >/dev/null \\\n"
            "    || useradd --create-home --uid 1001 --shell /usr/sbin/nologin appuser \\\n"
            "    || useradd --create-home --shell /usr/sbin/nologin appuser\n")


def split_deps(info, profile):
    pre = profile["preinstalled"]
    groups = {"apt": [], "pip": [], "pip_cpu": [], "npm": [],
              "manual": [], "composer": [], "drop": []}
    skipped = []
    for dep in dict.fromkeys(info["deps"]):
        if dep in pre:
            skipped.append(dep)
            continue
        kind = classify(dep)
        if kind == "pip" and dep in PIP_CPU_INDEX:
            groups["pip_cpu"].append(dep)
        else:
            groups[kind].append(dep)
    return groups, skipped


def render(vid, info):
    base = info["runtime"]
    profile = profile_for(base)
    role = profile["role"]
    pkg_mgr = detect_pkg_manager(base)
    groups, skipped = split_deps(info, profile)
    apt_pkgs = list(groups["apt"])
    pip_pkgs = list(groups["pip"])
    need_pip = bool(pip_pkgs or groups["pip_cpu"])

    sys_python = profile["system_python"] and need_pip
    if sys_python and "python3-pip" not in apt_pkgs:
        apt_pkgs.append("python3-pip")

    L = [f"FROM {base}", "", f"# {info['desc']}", f"# Variant id: {vid}"]
    if info["deps"]:
        L.append("# Declared dependencies: " + ", ".join(info["deps"]))
    if skipped:
        L.append("# Already provided by the base image (skipped): " + ", ".join(skipped))
    L.extend(VARIANT_NOTES.get(vid, []))
    L.append("")

    if profile["needs_root"]:
        L += ["# Base image defaults to a non-root user; switch to root to install.",
              "USER root", ""]

    if sys_python:
        L += ["# OS-managed python (PEP 668): let pip install into it.",
              "# (Honoured by pip>=23; silently ignored by older pip.)",
              "ENV PIP_BREAK_SYSTEM_PACKAGES=1", ""]

    warns = [p for p in apt_pkgs if p in WARN_PKGS]
    for p in warns:
        L += [f"# NOTE: '{p}' is a server daemon — usually run as a separate",
              "#       service/sidecar, not baked into an application image. Review."]
    if groups["drop"]:
        L.append(f"# NOTE: dropped {', '.join(groups['drop'])} "
                 "(ships inside ffmpeg; not a standalone package).")
    unverified = [p for p in apt_pkgs if is_unverified(p)]
    if unverified:
        L += [f"# REVIEW: unrecognised package name(s) sent to {pkg_mgr}: "
              f"{', '.join(unverified)}",
              "#         Verify they exist in this base's repos (or reclassify"
              " in the generator)."]
    if warns or groups["drop"] or unverified:
        L.append("")

    sys_block = apk_block(apt_pkgs) if pkg_mgr == "apk" else apt_block(apt_pkgs)
    if sys_block:
        L += [sys_block.rstrip("\n"), ""]

    if profile["php_exts"] and "libpq-dev" in apt_pkgs:
        L += ["# Headers alone do nothing in PHP: build the DB extensions.",
              "RUN docker-php-ext-install pdo_pgsql pdo_mysql", ""]

    if groups["composer"]:
        L += ["# Composer from the official image (Debian's 'composer' pkg is outdated).",
              "COPY --from=composer:2 /usr/bin/composer /usr/bin/composer", ""]

    if groups["manual"]:
        fixed = " ".join(p.replace("docker-cli", "docker-ce-cli")
                         for p in groups["manual"])
        L += [f"# TODO: {', '.join(groups['manual'])} are NOT in the default Debian repos.",
              "#       Add Docker's official apt repo before installing, or drop them.",
              "#       https://docs.docker.com/engine/install/debian/",
              "# RUN <add docker apt repo> && apt-get update \\",
              f"#     && apt-get install -y {fixed}", ""]

    cpu = pip_cpu_block(groups["pip_cpu"])
    if cpu:
        L += [cpu.rstrip("\n"), ""]
    pb = pip_block(pip_pkgs)
    if pb:
        L += [pb.rstrip("\n"), ""]
    nb = npm_block(groups["npm"])
    if nb:
        L += [nb.rstrip("\n"), ""]

    if profile["setup"]:
        L += list(profile["setup"]) + [""]

    if profile["run_as_root"]:
        L += ["# Runs as root: SDK/editor caches & licenses live under /root",
              "# (standard for CI build images of this kind).", ""]
    else:
        ub = user_block(pkg_mgr, role)
        if ub:
            L += [ub.rstrip("\n"), ""]

    L += ["WORKDIR /srv/app", ""]

    if role == "static-server":
        L.append("COPY nginx.conf /etc/nginx/conf.d/default.conf")
    L += ["COPY test_deliverables.py /opt/tests/test_deliverables.py",
          "RUN chmod +x /opt/tests/test_deliverables.py", ""]

    if role == "s6-service":
        L += ["# linuxserver/* uses s6-overlay; its /init entrypoint must NOT be",
              "# overridden. Run validation explicitly, e.g.:",
              "#   docker exec <container> \\",
              "#     blender --background --python /opt/tests/test_deliverables.py",
              "# (A GUI desktop image is a heavy fit for headless render-verify;",
              "#  a plain blender CLI base would be lighter.)",
              "",
              'VOLUME ["/srv/app"]']
        return "\n".join(L).rstrip() + "\n"

    L += ["COPY entrypoint.sh /entrypoint.sh",
          "RUN chmod +x /entrypoint.sh", "",
          'VOLUME ["/srv/app"]']

    if role == "static-server":
        L += ["EXPOSE 80",
              "HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=2 \\",
              "    CMD curl -f http://localhost:80/ || exit 1"]
        cmd = "serve"
    else:
        cmd = "validate"

    if not profile["run_as_root"]:
        L += ["USER appuser", ""]
    L += ['ENTRYPOINT ["/entrypoint.sh"]',
          f'CMD ["{cmd}"]']
    return "\n".join(L).rstrip() + "\n"
