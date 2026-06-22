"""Dockerfile builder for Fenrir task environments.

Pure-Python library (no Odoo dependencies) so it can be imported from
models/fenrir_generators.py at task-submit time. Given a base image and a
list of "key dependency" names, classify() sorts each dependency into the
right install channel (apt / apk / dnf / pip / npm / composer / project
manifest) and render() emits a Dockerfile for that base.

render() reconciles the install method with what the base actually provides:
if a pip/npm dependency is requested on a base that lacks Python/Node, the
matching toolchain is installed first using the base's own package manager.

This module is the single source of truth for that logic in Fenrir; there is
no separate CLI copy to keep in sync.
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
    # Web / backend frameworks + common Python libraries (route to pip, not apt).
    "flask", "django", "djangorestframework", "gunicorn", "celery",
    "starlette", "aiohttp", "tornado",
    "sqlalchemy", "psycopg2-binary", "pymongo",
    "httpx", "beautifulsoup4", "scrapy", "selenium",
    "jinja2", "pydantic", "python-dotenv",
    "flask-cors", "flask-sqlalchemy",
    # Machine learning / deep learning frameworks.
    "tensorflow", "keras", "jax", "xgboost", "lightgbm", "catboost",
    # DL ecosystem - CV / NLP / model training + Hugging Face stack.
    "timm", "ultralytics", "albumentations", "sentencepiece",
    "sentence-transformers", "tokenizers", "datasets", "accelerate",
    "diffusers", "spacy", "nltk", "gensim",
    # Data analysis / scientific computing / visualisation.
    "seaborn", "bokeh", "altair", "sympy", "networkx", "numba",
    "polars", "pyarrow", "h5py", "xarray", "dask",
    # Notebooks.
    "jupyter", "jupyterlab", "notebook", "ipykernel", "ipython",
    "nbconvert", "papermill",
    # LLM / AI APIs + experiment tracking + serving.
    "openai", "anthropic", "langchain", "tiktoken", "huggingface-hub",
    "mlflow", "wandb", "tensorboard", "gradio",
}
PIP_CPU_INDEX = {"torch", "torchvision"}

# npm packages that ship a global CLI binary — `npm install -g` is correct.
NPM = {
    "gltf-validator", "http-server", "serve", "live-server",
    # bundlers / build tools
    "vite", "webpack", "parcel", "rollup", "esbuild",
    # language / lint / format / test CLIs
    "typescript", "ts-node", "eslint", "prettier",
    "jest", "vitest", "mocha", "cypress",
    # CSS CLIs
    "sass", "tailwindcss",
    # framework build CLIs (`next build`, `nuxt build`)
    "next", "nuxt",
}

# npm RUNTIME libraries — imported by project code and resolved from the
# project's own node_modules. A global `npm install -g` lands in a prefix the
# build can't see, so these are NOT installed into the image; render() emits a
# note that they come from the task's package.json (npm ci) instead.
NPM_LIB = {
    "react", "react-dom", "vue", "svelte", "preact",
    "three", "babylonjs", "gsap", "framer-motion",
    "d3", "chart.js", "recharts", "axios",
    "bootstrap", "postcss",
    "express", "koa", "fastify",
    # mobile / desktop / state management (also project deps, from package.json)
    "react-native", "metro", "expo", "electron",
    "redux", "zustand", "react-router-dom", "styled-components",
    "socket.io-client",
}

# Common label -> canonical package name. Deps are normalised through this
# before classification, so names typed the way they appear in task briefs
# (three.js, next.js, tailwind, reactjs) install under their real npm/pip
# package name instead of being sent verbatim to the package manager.
ALIASES = {
    "three.js": "three",
    "threejs": "three",
    "next.js": "next",
    "nextjs": "next",
    "react.js": "react",
    "reactjs": "react",
    "vue.js": "vue",
    "vuejs": "vue",
    "tailwind": "tailwindcss",
    "tailwind-css": "tailwindcss",
    "tailwind css": "tailwindcss",
    "babylon.js": "babylonjs",
    "babylon": "babylonjs",
    "nuxt.js": "nuxt",
    "nuxtjs": "nuxt",
    "svelte.js": "svelte",
    "sveltejs": "svelte",
    "chartjs": "chart.js",
    "d3.js": "d3",
    "framer motion": "framer-motion",
    "framermotion": "framer-motion",
    "tensorflow-gpu": "tensorflow",
    "scikit_learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "opencv": "opencv-python-headless",
    "opencv-python": "opencv-python-headless",
    "cv2": "opencv-python-headless",
    "pytorch": "torch",
    "huggingface": "transformers",
    "hugging-face": "transformers",
}

MANUAL_APT = {"docker-cli", "docker-compose-plugin"}

DROP = {"ffprobe"}

COMPOSER = {"composer"}

KNOWN_APT = {
    "bash", "git", "curl", "wget", "unzip", "zip", "file", "make", "gcc", "g++",
    "build-essential", "chromium", "npm", "nodejs", "python3", "py3-pip",
    "postgresql-client", "redis-server", "redis-tools", "ffmpeg", "mediainfo",
    "imagemagick", "sox", "inkscape", "poppler-utils", "ghostscript",
    "libreoffice", "pandoc", "sqlite3", "xvfb", "mesa-utils",
    "default-mysql-client", "admesh", "assimp-utils", "openscad", "nginx", "php",
    # Common general-purpose system utilities — valid Debian/Ubuntu packages;
    # listing them keeps them out of the REVIEW false-positive net.
    "jq", "vim", "nano", "less", "tree", "htop", "procps", "ca-certificates",
    "gnupg", "tar", "gzip", "bzip2", "xz-utils", "zstd", "rsync", "sudo",
    "openssh-client", "iputils-ping", "dnsutils", "netcat-openbsd", "socat",
    "locales", "tzdata", "cron", "supervisor", "p7zip-full", "unrar",
    "coreutils", "findutils", "sed", "gawk", "patch", "ripgrep",
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
    if dep in NPM_LIB:
        return "npm_lib"
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
    "openjdk": {"role": "worker", "preinstalled": {"java", "jdk"}},
    "amazoncorretto": {"role": "worker", "preinstalled": {"java", "jdk"}},
    "maven": {"role": "worker", "preinstalled": {"maven", "mvn", "java", "jdk"}},
    # General app-dev language runtimes. The toolchain ships in the base image,
    # and library deps come from the project's own manifest at build time.
    "ruby": {"role": "worker", "preinstalled": {"ruby", "gem", "bundler"}},
    "golang": {"role": "worker", "preinstalled": {"go", "golang", "gofmt"}},
    "rust": {"role": "worker", "preinstalled": {"rust", "rustc", "cargo"}},
    "dart": {"role": "worker", "preinstalled": {"dart", "pub"}},
    "ghcr.io/cirruslabs/flutter": {"role": "worker",
                                   "preinstalled": {"flutter", "dart"}},
    "cirrusci/flutter": {"role": "worker", "preinstalled": {"flutter", "dart"}},
    "instrumentisto/flutter": {"role": "worker",
                               "preinstalled": {"flutter", "dart"}},
    # ML / data-science base images - the framework is already baked in, so the
    # generator skips re-installing it and only adds the task's extra deps.
    "tensorflow/tensorflow": {"role": "worker",
                              "preinstalled": {"tensorflow", "python3", "pip"}},
    "pytorch/pytorch": {"role": "worker",
                        "preinstalled": {"torch", "torchvision", "torchaudio",
                                         "python3", "pip"}},
    "huggingface/transformers-pytorch-gpu": {
        "role": "worker",
        "preinstalled": {"torch", "torchvision", "torchaudio", "transformers",
                         "python3", "pip"}},
    # Jupyter docker-stacks: canonical home is now quay.io/jupyter/*; the legacy
    # jupyter/* Docker Hub repos are frozen (stale) but still pull.
    "quay.io/jupyter/base-notebook": {
        "role": "worker",
        "preinstalled": {"python3", "pip", "jupyter", "notebook"}},
    "quay.io/jupyter/scipy-notebook": {
        "role": "worker",
        "preinstalled": {"python3", "pip", "jupyter", "notebook", "pandas",
                         "numpy", "scipy", "matplotlib", "scikit-learn"}},
    "quay.io/jupyter/tensorflow-notebook": {
        "role": "worker",
        "preinstalled": {"python3", "pip", "jupyter", "notebook", "tensorflow",
                         "pandas", "numpy"}},
    "jupyter/base-notebook": {
        "role": "worker",
        "preinstalled": {"python3", "pip", "jupyter", "notebook"}},
    "jupyter/scipy-notebook": {
        "role": "worker",
        "preinstalled": {"python3", "pip", "jupyter", "notebook", "pandas",
                         "numpy", "scipy", "matplotlib", "scikit-learn"}},
    "jupyter/tensorflow-notebook": {
        "role": "worker",
        "preinstalled": {"python3", "pip", "jupyter", "notebook", "tensorflow",
                         "pandas", "numpy"}},
    "continuumio/miniconda3": {"role": "worker",
                               "preinstalled": {"python3", "pip", "conda"}},
    "continuumio/anaconda3": {
        "role": "worker",
        "preinstalled": {"python3", "pip", "conda", "pandas", "numpy", "scipy",
                         "jupyter", "notebook", "matplotlib", "scikit-learn"}},
    "nvidia/cuda": {"role": "worker", "preinstalled": {"cuda"},
                    "system_python": True},
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
    b = base.lower()
    if "alpine" in b:
        return "apk"
    # RPM-family bases (RHEL/Fedora/Rocky/Alma/Amazon/Oracle/UBI) use dnf, not apt.
    if any(k in b for k in ("rockylinux", "almalinux", "centos", "fedora",
                            "amazonlinux", "oraclelinux", "/ubi", "redhat/ubi")):
        # The *-minimal RHEL/UBI images ship only microdnf, not full dnf.
        return "microdnf" if "minimal" in b else "dnf"
    return "apt"


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


def dnf_block(pkgs):
    return multiline("RUN dnf install -y", pkgs,
                     "&& dnf clean all") if pkgs else ""


def microdnf_block(pkgs):
    return multiline("RUN microdnf install -y", pkgs,
                     "&& microdnf clean all") if pkgs else ""


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
        if pkg_mgr == "apk":
            return ("# Non-root user + nginx runtime dirs (Alpine/BusyBox adduser)\n"
                    "RUN adduser -D -H -u 1001 -s /sbin/nologin appuser \\\n"
                    "    && chown -R appuser:appuser /var/cache/nginx /var/log/nginx \\\n"
                    "    && touch /var/run/nginx.pid \\\n"
                    "    && chown appuser:appuser /var/run/nginx.pid\n")
        return ("# Non-root user + nginx runtime dirs (Debian/RPM useradd)\n"
                "RUN useradd --system --no-create-home --uid 1001 \\\n"
                "        --shell /usr/sbin/nologin appuser \\\n"
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
    groups = {"apt": [], "pip": [], "pip_cpu": [], "npm": [], "npm_lib": [],
              "manual": [], "composer": [], "drop": []}
    skipped = []
    seen = set()
    for raw in info["deps"]:
        dep = ALIASES.get(raw, raw)
        if dep in seen:
            continue
        seen.add(dep)
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
    base = info.get("runtime")
    if not base:
        raise ValueError(
            "dockerfile_builder.render(): info['runtime'] (the base image) "
            "is required.")
    info = {**info, "desc": info.get("desc", ""),
            "deps": list(info.get("deps", []))}
    profile = profile_for(base)
    role = profile["role"]
    pkg_mgr = detect_pkg_manager(base)
    groups, skipped = split_deps(info, profile)
    apt_pkgs = list(groups["apt"])
    pip_pkgs = list(groups["pip"])
    pre = profile["preinstalled"]
    need_pip = bool(pip_pkgs or groups["pip_cpu"])
    need_npm = bool(groups["npm"])

    # ── Toolchain reconciliation ──────────────────────────────────────────
    # classify() picks an install *method* (pip / npm); make sure the base
    # actually provides that toolchain, installing it with the correct package
    # names for this base's package manager when it doesn't. This is what stops
    # `pip3 install` on a python-less base, `npm install -g` on a node-less
    # base, and the wrong `python3-pip` name on Alpine.
    python_available = bool(pre & {"python3", "python", "pip", "pip3", "conda"})
    provision_python = need_pip and not python_available
    if provision_python:
        py_toolchain = ("python3", "py3-pip") if pkg_mgr == "apk" \
            else ("python3", "python3-pip")
        for p in py_toolchain:
            if p not in apt_pkgs:
                apt_pkgs.append(p)

    node_available = bool(pre & {"node", "nodejs", "npm"})
    provision_node = need_npm and not node_available
    if provision_node:
        # On RHEL-family bases npm has no standalone package — it ships inside
        # the nodejs module's default profile — so install only `nodejs` there.
        node_toolchain = ("nodejs",) if pkg_mgr in ("dnf", "microdnf") \
            else ("nodejs", "npm")
        for p in node_toolchain:
            if p not in apt_pkgs:
                apt_pkgs.append(p)

    # The nginx HEALTHCHECK (static-server, below) shells out to curl — which
    # nginx base images do not ship — so make sure it gets installed.
    if role == "static-server" and "curl" not in apt_pkgs:
        apt_pkgs.append("curl")

    # pip-installing into a distro-managed python (PEP 668) needs the override.
    emit_pip_break = need_pip and (provision_python or profile["system_python"])

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

    if emit_pip_break:
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
    unverified = [p for p in groups["apt"] if is_unverified(p)]
    if unverified:
        L += [f"# REVIEW: unrecognised package name(s) sent to {pkg_mgr}: "
              f"{', '.join(unverified)}",
              "#         Verify they exist in this base's repos. If any is a"
              " language/framework library (a Maven, Gem, Go-module, Cargo,",
              "#         NuGet or pub package) it belongs in the project's own"
              " manifest (pom.xml / Gemfile / go.mod / Cargo.toml / .csproj /",
              "#         pubspec.yaml) installed by the build - not as a system"
              " key-dependency."]
    if warns or groups["drop"] or unverified:
        L.append("")

    if pkg_mgr == "apk":
        sys_block = apk_block(apt_pkgs)
    elif pkg_mgr == "dnf":
        sys_block = dnf_block(apt_pkgs)
    elif pkg_mgr == "microdnf":
        sys_block = microdnf_block(apt_pkgs)
    else:
        sys_block = apt_block(apt_pkgs)
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
    if groups["npm_lib"]:
        L += ["# Runtime npm libraries are NOT installed into the image; they are",
              "# resolved from the task's own package.json at build time (npm ci):",
              "#   " + ", ".join(groups["npm_lib"]), ""]

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

    L += ["COPY setup.sh /setup.sh",
          "RUN chmod +x /setup.sh", "",
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
    L += ['ENTRYPOINT ["/setup.sh"]',
          f'CMD ["{cmd}"]']
    return "\n".join(L).rstrip() + "\n"
