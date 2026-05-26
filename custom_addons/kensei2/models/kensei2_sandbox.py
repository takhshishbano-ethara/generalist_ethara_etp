import ast
import base64 as base64_mod
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError
from odoo.modules.registry import Registry

from .kensei2 import (
    _DEFAULT_LITELLM_CONFIG,
    _HEALTH_POLL_INTERVAL,
    _HEALTH_WAIT_TIMEOUT,
    _compose_cmd,
    _docker_available,
    _load_dotenv,
    _module_sandbox_dir,
    _wrap_messages_with_turn_feedback,
    _wrap_trajectory_message,
    generate_task_description_sync,
)

_logger = logging.getLogger(__name__)


def _parse_service_toml_fallback(path):
    """Minimal TOML parser for service.toml when tomllib/tomli unavailable."""
    result = {
        "name": "", "port": 0, "env_var_name": "", "healthcheck_path": "/health",
        "k8s_image": "", "cpu_request": "25m", "memory_request": "128Mi",
        "memory_limit": "256Mi",
    }
    key_map = {
        "service.name": "name",
        "service.port": "port",
        "service.env_var_name": "env_var_name",
        "service.healthcheck_path": "healthcheck_path",
        "k8s.image": "k8s_image",
        "k8s.cpu_request": "cpu_request",
        "k8s.memory_request": "memory_request",
        "k8s.memory_limit": "memory_limit",
    }
    section = ""
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                full_key = "%s.%s" % (section, key) if section else key
                if full_key in key_map:
                    mapped = key_map[full_key]
                    if mapped == "port":
                        try:
                            val = int(val)
                        except ValueError:
                            val = 0
                    result[mapped] = val
    return result if result["name"] else None


def _collect_mock_data_snapshot(env_dir, max_rows=3, max_services=10):
    """Read CSV/JSON data files from each mock API service dir.

    Returns a compact text snapshot of entity IDs and field structures
    so the test-generation LLM can write assertions grounded in real data.
    """
    import csv as _csv

    snapshot_parts = []
    service_count = 0

    for entry in sorted(os.listdir(env_dir)):
        svc_dir = os.path.join(env_dir, entry)
        if not os.path.isdir(svc_dir) or entry in ("skills", "__pycache__"):
            continue
        toml_path = os.path.join(svc_dir, "service.toml")
        if not os.path.isfile(toml_path):
            continue

        service_count += 1
        if service_count > max_services:
            break

        svc_lines = ["### %s" % entry]
        file_count = 0

        for fname in sorted(os.listdir(svc_dir)):
            fpath = os.path.join(svc_dir, fname)
            if not os.path.isfile(fpath):
                continue

            if fname.endswith(".csv"):
                try:
                    with open(fpath, "r", newline="", encoding="utf-8") as f:
                        reader = _csv.DictReader(f)
                        headers = reader.fieldnames or []
                        rows = []
                        for i, row in enumerate(reader):
                            if i >= max_rows:
                                break
                            rows.append(row)
                    if headers and rows:
                        svc_lines.append("**%s** — columns: %s" % (fname, ", ".join(headers)))
                        for row in rows[:2]:
                            compact = {k: v[:60] if isinstance(v, str) and len(v) > 60
                                        else v for k, v in list(row.items())[:8]}
                            svc_lines.append("  row: %s" % json.dumps(compact, ensure_ascii=False))
                        file_count += 1
                except Exception:
                    pass

            elif fname.endswith(".json") and not fname.endswith("_postman_collection.json"):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        keys = list(data.keys())[:10]
                        svc_lines.append("**%s** — top-level keys: %s" % (fname, ", ".join(keys)))
                        for k in keys[:3]:
                            val = data[k]
                            if isinstance(val, str) and len(val) > 80:
                                val = val[:80] + "..."
                            elif isinstance(val, (list, dict)):
                                val = "%s (len=%d)" % (type(val).__name__, len(val))
                            svc_lines.append("  %s: %s" % (k, val))
                    elif isinstance(data, list) and data:
                        svc_lines.append("**%s** — array of %d items" % (fname, len(data)))
                        if isinstance(data[0], dict):
                            svc_lines.append("  sample keys: %s" % ", ".join(list(data[0].keys())[:8]))
                    file_count += 1
                except Exception:
                    pass

        if file_count > 0:
            snapshot_parts.append("\n".join(svc_lines))

    if not snapshot_parts:
        return ""

    header = (
        "Below is a snapshot of the actual mock data files that each API service "
        "serves. Use these REAL entity IDs, field names, and values when writing "
        "assertions. Do NOT invent IDs or values — use the ones shown here or "
        "check via API calls in your tests.\n"
    )
    return header + "\n\n" + "\n\n".join(snapshot_parts)


# ──────────────────────────────────────────────────────────────────────
# Test generation lint validation (ported from kensei-harness)
# ──────────────────────────────────────────────────────────────────────
# Serialization-conflict retry helpers
#
# Postgres raises SQLSTATE 40001 ("could not serialize access due to
# concurrent update") whenever two transactions modify the same row under
# REPEATABLE READ isolation (Odoo's default). All background sandbox
# writers should funnel through these helpers so they cooperate
# gracefully instead of crashing the worker.
# ──────────────────────────────────────────────────────────────────────

_SERIALIZATION_RETRY_ATTEMPTS = int(os.getenv("KENSEI2_SERIALIZE_RETRY", "5"))
_SERIALIZATION_RETRY_BASE_DELAY = float(
    os.getenv("KENSEI2_SERIALIZE_DELAY", "0.15")
)


def _is_serialization_error(exc):
    """Return True if `exc` is (or wraps) a Postgres 40001 conflict."""
    if exc is None:
        return False
    pgcode = getattr(exc, "pgcode", None)
    if pgcode == "40001":
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc and _is_serialization_error(cause):
        return True
    msg = str(exc).lower()
    return (
        "could not serialize access" in msg
        or "serialization failure" in msg
    )


def _retry_with_cursor(
    db_name,
    fn,
    *,
    label="",
    max_attempts=None,
    base_delay=None,
):
    """Run `fn(env)` in a fresh Registry cursor, retrying on serialization conflicts.

    The cursor auto-commits on clean exit; on a 40001 it rolls back and we
    retry with jittered exponential back-off. The jitter is important: two
    writers that collide once will, without it, re-collide in lockstep on
    every retry. Other exceptions propagate normally.
    """
    if max_attempts is None:
        max_attempts = _SERIALIZATION_RETRY_ATTEMPTS
    if base_delay is None:
        base_delay = _SERIALIZATION_RETRY_BASE_DELAY

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                return fn(env)
        except Exception as exc:
            last_exc = exc
            if _is_serialization_error(exc) and attempt < max_attempts:
                # Equal-jitter back-off: half fixed, half random.
                window = base_delay * (2 ** (attempt - 1))
                delay = window / 2 + random.uniform(0, window / 2)
                _logger.warning(
                    "[SERIALIZE-RETRY] %s — conflict on attempt %d/%d, "
                    "retrying in %.2fs",
                    label or "anonymous",
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            raise
    if last_exc is not None:
        raise last_exc

MODEL_TYPES = [
    ("claude", "Claude Opus 4.7"),
    ("gpt", "GPT-5.5"),
]

MODEL_DEFAULTS = {
    "claude": "litellm/claude-opus-4.7",
    "gpt": "litellm/gpt-5.5",
}

# Batch execution pool — supports parallel deploy/prompt/stop for 16+ pods
_BATCH_POOL_WORKERS = int(os.getenv("BATCH_POOL_WORKERS", "20"))
_BATCH_POOL = ThreadPoolExecutor(
    max_workers=_BATCH_POOL_WORKERS, thread_name_prefix="kensei2-batch"
)
_POD_MAX_RETRIES = int(os.getenv("BATCH_POD_MAX_RETRIES", "2"))
_BATCH_START_TIMEOUT = int(os.getenv("BATCH_START_TIMEOUT", "600"))
_BATCH_WAVE_SIZE = int(os.getenv("BATCH_WAVE_SIZE", "4"))
_BATCH_WAVE_DELAY = int(os.getenv("BATCH_WAVE_DELAY", "15"))

GATEWAY_PORT_BASE = 21000
LITELLM_PORT_BASE = 16000
DB_PORT_BASE = 17432

TRAJECTORY_FIELD_MAP = {
    "claude": "claude_trajectory",
    "gpt": "gpt_trajectory",
    "1pa": "onePA_trajectory",
    "1pb": "onePB_trajectory",
    "1pc": "onePC_trajectory",
    "1pd": "onePD_trajectory",
}


# ──────────────────────────────────────────────────────────────────────
# Distractor skill computation (ported from kensei-harness)
# ──────────────────────────────────────────────────────────────────────

DOMAIN_TAGS = {
    "amazon-seller-api":    ("commerce", "retail"),
    "etsy-api":             ("commerce", "retail", "creative"),
    "pinterest-api":        ("social", "media", "creative"),
    "instagram-api":        ("social", "media", "creative"),
    "youtube-api":          ("social", "media"),
    "linear-api":           ("productivity", "saas"),
    "quickbooks-api":       ("finance", "saas"),
    "google-classroom-api": ("productivity", "education"),
    "myfitnesspal-api":     ("health", "lifestyle"),
    "ring-api":             ("iot", "lifestyle"),
}

ALL_API_NAMES = sorted(DOMAIN_TAGS.keys())

_API_PROMPT_KEYWORDS = {
    "amazon-seller-api":    ("amazon", "seller", "asin", "sku", "fba", "seller central"),
    "etsy-api":             ("etsy", "listing", "shop", "handmade", "craft", "woodwork", "woodcraft"),
    "pinterest-api":        ("pinterest", "pin", "board"),
    "instagram-api":        ("instagram", "insta", "ig ", "ig,", "post", "reel", "story", "stories", "media"),
    "youtube-api":          ("youtube", "video", "channel", "subscriber", "playlist", "upload"),
    "linear-api":           ("linear", "issue", "project management", "sprint", "backlog", "ticket"),
    "quickbooks-api":       ("quickbooks", "invoice", "accounting", "expense", "bill", "payment", "ledger"),
    "google-classroom-api": ("classroom", "course", "assignment", "student", "teacher", "grading"),
    "myfitnesspal-api":     ("myfitnesspal", "fitness", "calorie", "exercise", "workout", "nutrition", "diet", "meal", "run ", "running"),
    "ring-api":             ("ring", "doorbell", "camera", "security", "motion"),
}

DISTRACTOR_COUNT = 4


def _infer_required_apis_from_prompt(prompt):
    """Infer which mock APIs a task prompt requires by keyword matching.

    Returns a sorted list of API names (e.g. ['etsy-api', 'youtube-api']).
    """
    prompt_lower = prompt.lower()
    required = []
    for api_name, keywords in _API_PROMPT_KEYWORDS.items():
        if any(kw in prompt_lower for kw in keywords):
            required.append(api_name)
    return sorted(required)


def _compute_distractor_skills(required_apis, task_id, count=DISTRACTOR_COUNT):
    """Pick distractor APIs that are NOT required but share domain tags.

    Ported from kensei-harness _compute_distractor_skills.
    Uses deterministic seed (task_id) for reproducibility.
    """
    required_set = set(required_apis)
    required_tags = set()
    for api in required_apis:
        required_tags.update(DOMAIN_TAGS.get(api, ()))

    domain_pool = sorted(
        api for api in ALL_API_NAMES
        if api not in required_set
        and set(DOMAIN_TAGS.get(api, ())) & required_tags
    )

    if len(domain_pool) < count:
        leftover = sorted(
            api for api in ALL_API_NAMES
            if api not in required_set and api not in domain_pool
        )
        domain_pool = domain_pool + leftover

    rng = random.Random(task_id or "kensei2-default")
    rng.shuffle(domain_pool)
    return domain_pool[:count]


# ──────────────────────────────────────────────────────────────────────
# Test generation lint validation (ported from kensei-harness)
# ──────────────────────────────────────────────────────────────────────

MAX_TESTGEN_ATTEMPTS = 3

_ALLOWED_WEIGHTS = {50, 30, 10, -10, -30, -50}

_FORBIDDEN_POLARITY_PATTERNS = (
    re.compile(r"\bassert\s+not\b"),
    re.compile(r"==\s*0\b"),
    re.compile(r"\bis\s+None\b"),
    re.compile(r"\bnot\s+in\b"),
)

_LAZY_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be", "been",
    "this", "that", "these", "those", "it", "its", "they", "them", "their",
    "data", "file", "files", "value", "values", "row", "rows", "column",
    "result", "results", "report", "output", "input", "name", "type",
    "code", "id", "test", "check", "task", "user", "item", "field", "key",
    "list", "table", "text", "info", "details", "summary", "content",
    "response", "request", "true", "false", "none", "null", "yes", "no",
    "object", "array", "string", "number", "json", "csv", "header",
    "line", "lines", "page", "section", "title", "label",
    "html", "body", "tr", "td", "th", "div", "span",
    "new", "old", "all", "some", "any", "each", "many", "more", "most",
    "only", "also", "just", "very", "much", "such",
    "make", "use", "see", "show", "set", "get", "find", "go", "run",
})

_TRIVIALITY_PATTERNS = (
    re.compile(r"len\s*\(\s*lines\s*\)\s*>=?\s*\d"),
    re.compile(r"len\s*\(\s*content\s*\)\s*>\s*0"),
    re.compile(r"getsize\s*\([^)]+\)\s*>\s*0"),
    re.compile(r"len\s*\([^)]+\)\s*>\s*0\b"),
)

_ALLOWED_IMPORTS = frozenset({
    "json", "os", "subprocess", "sqlite3", "urllib", "pytest", "hashlib",
    "re", "csv", "io", "pathlib", "struct", "base64", "datetime", "math",
    "collections", "itertools", "functools", "string", "textwrap",
    "xml", "zipfile", "gzip", "shutil", "glob", "tempfile", "copy",
})

_SAFE_FALLBACK_STUB = '''\
class TestBehavioralFallback:
    """Fallback: testgen LLM produced unparseable output after all retries."""

    def test_placeholder(self):
        assert True


class TestNegativeWeightFallback:
    """Negative weight fallback stub."""

    def test_placeholder_negative(self):
        assert True
'''


def _collect_test_functions(tree):
    """Collect all test_* FunctionDef nodes from an AST tree."""
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]


def _function_has_assert(func):
    """Check if a function AST node contains at least one assert statement."""
    return any(isinstance(n, ast.Assert) for n in ast.walk(func))


def _function_passes_empty_files(func):
    """L16: True if every assert in the function is just file_exists(...)."""
    asserts = [n for n in ast.walk(func) if isinstance(n, ast.Assert)]
    if not asserts:
        return False
    for node in asserts:
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is True
        ):
            test = test.left
        if not (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Name)
            and test.func.id == "file_exists"
        ):
            return False
    return True


def _auto_repair_truncated_python(code):
    """Close unbalanced strings/brackets at EOF so truncated LLM output parses.

    Returns repaired code on success, or None if unrepairable.
    """
    if not code:
        return None
    try:
        ast.parse(code)
        return code
    except SyntaxError:
        pass

    pairs = {"(": ")", "[": "]", "{": "}"}

    def _scan(src):
        stack = []
        in_s = False
        trp = False
        q = ""
        start = -1
        i = 0
        n = len(src)
        while i < n:
            ch = src[i]
            if in_s:
                if trp:
                    if src[i:i + 3] == q * 3:
                        in_s = False
                        trp = False
                        i += 3
                        continue
                    i += 1
                    continue
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == q:
                    in_s = False
                elif ch == "\n":
                    in_s = False
                i += 1
                continue
            if ch == "#":
                while i < n and src[i] != "\n":
                    i += 1
                continue
            if ch in ('"', "'"):
                if src[i:i + 3] in ('"""', "'''"):
                    in_s = True
                    trp = True
                    q = ch
                    start = i
                    i += 3
                    continue
                in_s = True
                trp = False
                q = ch
                start = i
                i += 1
                continue
            if ch in "([{":
                stack.append(pairs[ch])
            elif ch in ")]}":
                if stack and stack[-1] == ch:
                    stack.pop()
            i += 1
        return in_s, trp, q, start, stack

    in_string, triple, quote, str_start, bracket_stack = _scan(code)

    suffix = ""
    if in_string:
        suffix = (quote * 3) if triple else quote
    while bracket_stack:
        suffix += bracket_stack.pop()

    if suffix:
        repaired = code + suffix
        try:
            ast.parse(repaired)
            return repaired
        except SyntaxError:
            pass

    if in_string and str_start >= 0:
        trunc = code[:str_start].rstrip()
        while trunc and trunc[-1] in ", \t\n":
            trunc = trunc[:-1]
        in_s2, _, _, _, stack2 = _scan(trunc)
        if not in_s2:
            suffix2 = ""
            while stack2:
                suffix2 += stack2.pop()
            repaired = trunc + suffix2
            try:
                ast.parse(repaired)
                return repaired
            except SyntaxError:
                pass

    return None


def _self_validate_tests(code, weights, has_api_services=False, distractor_apis=None):
    """Run deterministic lints on generated test code. Returns list of failure strings.

    Ported from kensei-harness L1-L24 (subset applicable without extracted values).
    Empty list means the draft passes all lints.
    """
    failures = []

    # L15: must parse — if not, none of the other lints can run
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ["L15: emitted code is not valid Python: %s" % exc]

    # L1: forbidden polarity (Convention B)
    seen_polarity = set()
    for pat in _FORBIDDEN_POLARITY_PATTERNS:
        for m in pat.finditer(code):
            tok = m.group(0)
            if tok not in seen_polarity:
                seen_polarity.add(tok)
                failures.append(
                    "L1: forbidden assertion polarity '%s' — rephrase positively, "
                    "encode bad behavior with a negative weight" % tok
                )

    # L3: lazy substrings
    lazy_pattern = re.compile(r'"([a-z]{2,})"\s+in\s+[A-Za-z_][\w.\[\]]*(?:\.lower\(\))?')
    lazy_hits = []
    for m in lazy_pattern.finditer(code):
        word = m.group(1).lower()
        if word in _LAZY_STOPWORDS:
            lazy_hits.append(word)
    if lazy_hits:
        failures.append(
            "L3: lazy single-word substring assertion(s) on common stopwords: %s. "
            "Assert on specific deterministic values instead." % sorted(set(lazy_hits))[:5]
        )

    # L4: weights integrity
    if weights:
        bad = {n: w for n, w in weights.items() if w not in _ALLOWED_WEIGHTS}
        if bad:
            failures.append("L4: weight values outside the allowed set {50,30,10,-10,-30,-50}: %s" % bad)

    # L5: class prefix invariants
    class_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    bad_classes = [c for c in class_names if not (
        c.startswith("TestBehavioral") or c.startswith("TestOutcome") or c.startswith("TestNegativeWeight")
    )]
    if bad_classes:
        failures.append(
            "L5: class names not matching required prefixes "
            "(TestBehavioral*, TestOutcome*, TestNegativeWeight*): %s" % bad_classes
        )

    # L7: TestNegativeWeight required always
    if not any(c.startswith("TestNegativeWeight") for c in class_names):
        failures.append("L7: no TestNegativeWeight* class emitted — mandatory for every task")

    # L8: at least one SERVICE_URL ref when APIs listed
    if has_api_services and not re.search(r"\b[A-Z][A-Z0-9_]*_URL\b", code):
        failures.append(
            "L8: API services are listed but no <SERVICE>_URL constant is referenced"
        )

    # L12: triviality patterns
    for pat in _TRIVIALITY_PATTERNS:
        m = pat.search(code)
        if m:
            failures.append(
                "L12: triviality pattern '%s' — assert on a specific value, not on existence/non-emptiness"
                % m.group(0)
            )
            break

    # L14: every test function must have at least one assert
    test_funcs = _collect_test_functions(tree)
    no_assert = [f.name for f in test_funcs if not _function_has_assert(f)]
    if no_assert:
        failures.append("L14: test function(s) with NO assert statement: %s" % no_assert)

    # L16: no-op exploit — file_exists-only tests should not exceed 25% of positive weight
    if weights:
        passes_empty = []
        for func in test_funcs:
            if _function_passes_empty_files(func):
                passes_empty.append(func.name)
        total_positive = sum(w for w in weights.values() if w > 0)
        passes_empty_positive = sum(
            weights.get(name, 0) for name in passes_empty if weights.get(name, 0) > 0
        )
        if total_positive > 0:
            ratio = passes_empty_positive / total_positive
            if ratio >= 0.25:
                failures.append(
                    "L16: no-op exploit risk — tests that pass on empty files sum to "
                    "%d/%d positive weight (%.0f%%); replace file_exists-only tests with "
                    "content/value assertions: %s" % (
                        passes_empty_positive, total_positive, ratio * 100, passes_empty[:5]
                    )
                )

    # L21: forbidden imports
    forbidden_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod not in _ALLOWED_IMPORTS:
                    forbidden_imports.append(mod)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                if mod not in _ALLOWED_IMPORTS:
                    forbidden_imports.append(mod)
    if forbidden_imports:
        failures.append(
            "L21: forbidden import(s) not available in verifier environment: %s. "
            "Only stdlib modules are available." % sorted(set(forbidden_imports))
        )

    # L22: API response shape misuse — iterating api_get/api_post directly
    audit_iter_pattern = re.compile(r"for\s+\w+\s+in\s+(api_get|api_post)\s*\([^)]*\)\s*")
    if audit_iter_pattern.search(code):
        failures.append(
            "L22: potential dict-as-list iteration — code directly iterates the return of "
            "api_get/api_post. Assign to a variable first, unwrap with .get('results', data) "
            "for business endpoints."
        )

    # L24: paginated API response envelope misuse
    if has_api_services:
        api_call_pat = re.compile(r'(\w+)\s*=\s*api_get\s*\([^)]*"/v1/[^"]*"[^)]*\)')
        unwrap_pat = re.compile(r'\.get\s*\(\s*["\']results["\']\s*[,)]')
        paginated_misuse = []
        for match in api_call_pat.finditer(code):
            var_name = match.group(1)
            after = code[match.end():match.end() + 500]
            isinstance_check = re.search(
                r"assert\s+isinstance\s*\(\s*%s\s*,\s*list\s*\)" % re.escape(var_name),
                after,
            )
            if isinstance_check:
                has_unwrap = unwrap_pat.search(code[match.start():match.end() + 500])
                if not has_unwrap:
                    paginated_misuse.append(var_name)
        if paginated_misuse:
            failures.append(
                "L24: paginated API response not unwrapped — %s use "
                "isinstance(var, list) directly on api_get result without handling "
                "the paginated envelope. Use: data.get('results', data) if isinstance(data, dict) "
                "else data" % paginated_misuse[:5]
            )

    # L25: specific-value literal assertions on API response fields
    # Detects `assert obj["field"] == "some string"` or `== 29.99` patterns
    # that will fail when mock data differs from LLM's guesses.
    # Exemptions: status codes, booleans, small integers (0-5), None checks,
    # and known stable fields (status, type, kind, method).
    _STABLE_FIELD_NAMES = {"status", "type", "kind", "method", "media_type", "status_code"}
    literal_assertions = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        if len(test.ops) != 1 or not isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
            continue
        comparator = test.comparators[0]
        if not isinstance(comparator, ast.Constant):
            continue
        val = comparator.value
        if isinstance(val, bool) or val is None:
            continue
        if isinstance(val, int) and -1 <= val <= 5:
            continue
        left = test.left
        field_name = ""
        if isinstance(left, ast.Subscript) and isinstance(left.slice, ast.Constant):
            field_name = str(left.slice.value).lower()
        if field_name in _STABLE_FIELD_NAMES:
            continue
        if isinstance(val, str) and len(val) > 3:
            literal_assertions.append('"%s"' % (val[:30] + "..." if len(val) > 30 else val))
        elif isinstance(val, float):
            literal_assertions.append(str(val))
        elif isinstance(val, int):
            literal_assertions.append(str(val))

    if len(literal_assertions) > 3:
        failures.append(
            "L25: %d assertions compare API response fields to specific literal values: "
            "%s — these WILL FAIL if mock data differs. Use type/range/presence checks "
            "instead: isinstance(val, str), val > 0, 'key' in obj. Only assert exact "
            "literals for values stated in the task instruction."
            % (len(literal_assertions), ", ".join(literal_assertions[:5]))
        )

    if distractor_apis:
        code_lower = code.lower()
        neg_class_methods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.startswith("TestNegativeWeight"):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"):
                        neg_class_methods.add(item.name.lower())
        neg_code = " ".join(neg_class_methods)
        uncovered = []
        for api in distractor_apis:
            short = api.replace("-api", "").replace("-", "_")
            const = api.upper().replace("-", "_") + "_URL"
            if short not in neg_code and const.lower() not in code_lower:
                uncovered.append(api)
        if uncovered:
            failures.append(
                "L26: missing TestNegativeWeight* coverage for %d distractor API(s): %s "
                "— add at least one negative test per distractor that checks /audit/summary."
                % (len(uncovered), ", ".join(uncovered))
            )

    return failures


def _mark_task_description_status(db_name, task_id, field_name, status, entry_index=-1):
    """Update the task_description_status on a trajectory entry."""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if not task.exists():
                return
            raw = task[field_name] or ""
            if not raw.strip():
                return
            data = json.loads(raw)
            if isinstance(data, list) and data:
                idx = entry_index if 0 <= entry_index < len(data) else -1
                if data[idx].get("task_description_status") == "aborted":
                    return
                data[idx]["task_description_status"] = status
                task.write({field_name: json.dumps(data, indent=2, ensure_ascii=False)})
    except Exception:
        _logger.exception(
            "Failed to mark task_description_status=%s for %s task %s",
            status,
            field_name,
            task_id,
        )


def _inject_task_description_bg(
    db_name, task_id, field_name, seed_prompt, messages, entry_index=-1
):
    """Background: generate task description via GLM and inject into saved trajectory."""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            desc, usage = generate_task_description_sync(env, seed_prompt, messages)
            if usage.get("input_tokens", 0) > 0 or usage.get("output_tokens", 0) > 0:
                task_rec = env["kensei2.kensei2"].browse(task_id)
                if task_rec.exists():
                    task_rec.write(
                        {
                            "taskdesc_input_tokens": (
                                task_rec.taskdesc_input_tokens or 0
                            )
                            + usage.get("input_tokens", 0),
                            "taskdesc_output_tokens": (
                                task_rec.taskdesc_output_tokens or 0
                            )
                            + usage.get("output_tokens", 0),
                        }
                    )
            if not desc:
                _mark_task_description_status(
                    db_name, task_id, field_name, "done", entry_index
                )
                return
            task = env["kensei2.kensei2"].browse(task_id)
            if not task.exists():
                return
            raw = task[field_name] or ""
            if not raw.strip():
                return
            data = json.loads(raw)
            if isinstance(data, list) and data:
                idx = entry_index if 0 <= entry_index < len(data) else -1
                if data[idx].get("task_description_status") == "aborted":
                    return
                mi = data[idx].setdefault("trajectory", {}).setdefault("meta_info", {})
                mi["task_description"] = desc
                mi["task_completion_status"] = "success"
                data[idx]["task_description_status"] = "done"
            elif isinstance(data, dict):
                mi = data.setdefault("meta_info", {})
                mi["task_description"] = desc
                mi["task_completion_status"] = "success"
            task.write({field_name: json.dumps(data, indent=2, ensure_ascii=False)})
            _logger.info(
                "Injected task_description (%d chars) into %s for task %s",
                len(desc),
                field_name,
                task_id,
            )
    except Exception:
        _logger.exception(
            "Failed to inject task_description into %s for task %s",
            field_name,
            task_id,
        )
        _mark_task_description_status(db_name, task_id, field_name, "done", entry_index)


def _generate_intent_tests_background(db_name, sandbox_id, prompt):
    """Background worker: generate intent-based tests (parallel with pod deploy)."""
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            sandbox = env["kensei2.sandbox"].browse(sandbox_id)
            if sandbox.exists():
                sandbox._generate_intent_tests(prompt)
    except Exception:
        _logger.exception(
            "Background intent test generation failed (sandbox=%s)", sandbox_id
        )


_STRIP_IMPORT_RE = re.compile(
    r"^(?:import\s+\w+|from\s+\w[\w.]*\s+import\s+.*)$",
    re.MULTILINE,
)
_STRIP_HELPER_RE = re.compile(
    r"^def\s+(?:_get|_post|_request|api_get|api_post|read_file|file_exists)\s*\(.*?(?=\nclass\s|\ndef\s[^_]|\Z)",
    re.MULTILINE | re.DOTALL,
)
_STRIP_ENVIRON_RE = re.compile(
    r"^[A-Z_]+_URL\s*=\s*os\.environ.*$",
    re.MULTILINE,
)


def _sanitize_llm_test_code(code):
    code = _STRIP_IMPORT_RE.sub("", code)
    code = _STRIP_ENVIRON_RE.sub("", code)
    code = _STRIP_HELPER_RE.sub("", code)
    code = re.sub(r"\n{4,}", "\n\n\n", code)
    return code.strip()


def _generate_task_tests_background(db_name, task_id):
    try:
        from odoo.modules.module import get_module_path

        module_path = get_module_path("kensei2")

        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if not task.exists():
                _logger.error("[TASK-TESTGEN] task %s does not exist", task_id)
                return

            task.write({"test_code_status": "generating"})

            prompt = (task.batch_prompt or task.initial_prompt or "").strip()
            task_toml = ""
            try:
                task_toml = task._build_harbor_task_toml() or ""
            except Exception:
                pass

            ICP = env["ir.config_parameter"].sudo()
            inference_arn = (
                ICP.get_param("kensei2.test_gen_inference_arn")
                or ICP.get_param("kensei2.bedrock_inference_arn")
                or ""
            ).strip()
            region = (ICP.get_param("kensei2.bedrock_region") or "ap-south-1").strip()

            dotenv = _load_dotenv()
            api_key = (
                dotenv.get("KENSEI2_AWS_BEARER_TOKEN")
                or dotenv.get("AWS_BEARER_TOKEN_BEDROCK", "")
            ).strip()

        if not prompt:
            raise RuntimeError("No prompt available for test generation")
        if not api_key:
            raise RuntimeError("KENSEI2_AWS_BEARER_TOKEN (or AWS_BEARER_TOKEN_BEDROCK) not set")
        if not inference_arn:
            raise RuntimeError("Bedrock Inference ARN not configured in Settings > Kensei2")

        env_dir = os.path.join(module_path, "environment")
        services = {}
        if os.path.isdir(env_dir):
            for entry in sorted(os.listdir(env_dir)):
                svc_dir = os.path.join(env_dir, entry)
                toml_path = os.path.join(svc_dir, "service.toml")
                if os.path.isdir(svc_dir) and os.path.isfile(toml_path):
                    svc_meta = _parse_service_toml_fallback(toml_path)
                    if svc_meta:
                        services[svc_meta["name"]] = {
                            "env_var": svc_meta["env_var_name"],
                            "port": svc_meta["port"],
                        }

        api_docs = ""
        api_docs_path = os.path.join(module_path, "environment", "API_DOCUMENTATION.md")
        if os.path.isfile(api_docs_path):
            with open(api_docs_path, "r") as f:
                api_docs = f.read()
            if len(api_docs) > 30000:
                api_docs = api_docs[:30000] + "\n\n... [truncated]"

        prompt_file = os.path.join(module_path, "test_generation_system_prompt.md")
        if not os.path.isfile(prompt_file):
            raise RuntimeError("test_generation_system_prompt.md not found in kensei2 module")
        with open(prompt_file, "r") as f:
            system_prompt = f.read()

        has_api_services = bool(services)

        task_identifier = ""
        with Registry(db_name).cursor() as cr2:
            env2 = api.Environment(cr2, SUPERUSER_ID, {})
            t2 = env2["kensei2.kensei2"].browse(task_id)
            task_identifier = t2.task_id or "kensei2/%s" % t2.id

        required_apis = _infer_required_apis_from_prompt(prompt) if prompt else []
        if not required_apis and has_api_services:
            required_apis = sorted(services.keys())
        distractor_apis = _compute_distractor_skills(
            required_apis, task_identifier
        ) if required_apis else []

        _logger.info(
            "[TASK-TESTGEN] task %s — required=%s distractors=%s",
            task_id, required_apis, distractor_apis,
        )

        data_snapshot = ""
        if has_api_services:
            try:
                data_snapshot = _collect_mock_data_snapshot(env_dir)
            except Exception:
                _logger.debug("[TASK-TESTGEN] data snapshot collection failed", exc_info=True)

        wrapper_lines = [
            '"""',
            "Auto-generated test suite for verifying API state changes and task completion.",
            '"""',
            "",
            "import json",
            "import os",
            "import subprocess",
            "import sqlite3",
            "from urllib.request import Request, urlopen",
            "",
            "import pytest",
            "",
        ]
        for svc_name, info in services.items():
            const_name = svc_name.upper().replace("-", "_") + "_URL"
            wrapper_lines.append(
                '%s = os.environ.get("%s", "http://localhost:%d")'
                % (const_name, info["env_var"], info["port"])
            )
        wrapper_lines.extend(
            [
                "",
                "",
                "def _request(method, url, data=None):",
                '    body = None',
                '    headers = {"Accept": "application/json"}',
                "    if data is not None:",
                '        body = json.dumps(data).encode("utf-8")',
                '        headers["Content-Type"] = "application/json"',
                "    req = Request(url, data=body, method=method, headers=headers)",
                "    with urlopen(req, timeout=30) as resp:",
                '        return json.loads(resp.read().decode("utf-8"))',
                "",
                "",
                "def api_get(base_url, endpoint):",
                '    """Two-arg helper: api_get(BASE_URL, "/path")."""',
                '    return _request("GET", f"{base_url}{endpoint}")',
                "",
                "",
                "def api_post(base_url, endpoint, data=None):",
                '    """Two-arg helper: api_post(BASE_URL, "/path", {...})."""',
                '    return _request("POST", f"{base_url}{endpoint}", data=data)',
                "",
                "",
                "# Compatibility aliases — accept a full URL (one argument)",
                "def _get(url):",
                '    """One-arg helper: _get(f"{BASE_URL}/path")."""',
                '    return _request("GET", url)',
                "",
                "",
                "def _post(url, data=None):",
                '    """One-arg helper: _post(f"{BASE_URL}/path", {...})."""',
                '    return _request("POST", url, data=data)',
                "",
                "",
                "def read_file(path):",
                "    with open(path) as f:",
                "        return f.read()",
                "",
                "",
                "def file_exists(path):",
                "    return os.path.exists(path)",
                "",
                "",
            ]
        )
        wrapper_prefix = "\n".join(wrapper_lines)

        from ..controllers.llm_assisst_qc import _call_bedrock_converse

        gen_start = time.time()
        best_code = ""
        best_weights = {}
        best_failures = []
        lint_failures = []
        total_usage = {"input_tokens": 0, "output_tokens": 0}

        for attempt in range(1, MAX_TESTGEN_ATTEMPTS + 1):
            msg = []
            msg.append("## Task Instruction (instruction.md)\n")
            msg.append(
                "Generate tests that verify the agent performed these actions correctly.\n\n"
            )
            msg.append(prompt[:8000] if len(prompt) > 8000 else prompt)
            msg.append("\n")

            if task_toml:
                msg.append("\n## task.toml (metadata)\n")
                msg.append("```toml\n%s\n```\n" % task_toml)

            msg.append("\n## Available Mock API Services\n")
            if services:
                for svc_name, info in services.items():
                    const_name = svc_name.upper().replace("-", "_") + "_URL"
                    tag = ""
                    if svc_name in required_apis:
                        tag = " **(REQUIRED — task uses this API)**"
                    elif svc_name in distractor_apis:
                        tag = " **(DISTRACTOR — agent should NOT touch this)**"
                    msg.append(
                        "- `%s` (env: `%s`, port %d) → use constant `%s`%s\n"
                        % (svc_name, info["env_var"], info["port"], const_name, tag)
                    )
            else:
                msg.append("No API services configured.\n")

            if required_apis:
                msg.append("\n## Required APIs (agent MUST use these)\n")
                for api_name in required_apis:
                    msg.append("- `%s`\n" % api_name)

            if distractor_apis:
                msg.append("\n## Distractor APIs (agent must NOT touch — generate TestNegativeWeight* for each)\n")
                for api_name in distractor_apis:
                    const_name = api_name.upper().replace("-", "_") + "_URL"
                    msg.append("- `%s` → constant `%s`\n" % (api_name, const_name))

            msg.append("\n## Mock API Documentation (endpoints for verification)\n")
            msg.append(api_docs)

            if data_snapshot:
                msg.append("\n\n## Mock Data Snapshot (REAL entity IDs and field values)\n")
                msg.append(data_snapshot)

            if lint_failures:
                if attempt >= 2:
                    lint_failures = [
                        "THIS IS RETRY %d/%d. The previous draft had these issues. "
                        "Read each lint message LITERALLY. Do NOT repeat the same mistakes."
                        % (attempt, MAX_TESTGEN_ATTEMPTS),
                        "",
                    ] + lint_failures
                msg.append("\n\n## LINT FAILURES FROM PREVIOUS ATTEMPT (fix ALL of these)\n")
                for fail in lint_failures:
                    msg.append("- %s\n" % fail)

            user_message = "\n".join(msg)

            try:
                response_text, usage = _call_bedrock_converse(
                    api_key=api_key,
                    inference_arn=inference_arn,
                    region=region,
                    system_prompt=system_prompt,
                    user_message=user_message,
                    max_tokens=12000,
                    temperature=0.2,
                    timeout=300.0,
                )
            except Exception as exc:
                _logger.warning(
                    "[TASK-TESTGEN] LLM call failed on attempt %d/%d for task %s: %s",
                    attempt, MAX_TESTGEN_ATTEMPTS, task_id, exc,
                )
                if best_code:
                    break
                continue

            total_usage["input_tokens"] += usage.get("input_tokens", 0)
            total_usage["output_tokens"] += usage.get("output_tokens", 0)

            cleaned = response_text.strip()
            if cleaned.startswith("```"):
                try:
                    first_nl = cleaned.index("\n")
                    cleaned = cleaned[first_nl + 1:]
                except ValueError:
                    pass
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3].strip()

            json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not json_match:
                _logger.warning(
                    "[TASK-TESTGEN] No JSON in LLM response on attempt %d for task %s",
                    attempt, task_id,
                )
                lint_failures = ["No JSON object found in LLM response — emit valid {code, weights} JSON"]
                continue

            try:
                parsed = json.loads(json_match.group(0))
            except json.JSONDecodeError as exc:
                _logger.warning(
                    "[TASK-TESTGEN] JSON parse failed on attempt %d for task %s: %s",
                    attempt, task_id, exc,
                )
                lint_failures = ["JSON parse error: %s — emit valid JSON" % exc]
                continue

            if not isinstance(parsed, dict):
                lint_failures = ["Expected JSON object with code+weights, got %s" % type(parsed).__name__]
                continue

            llm_code = parsed.get("code", "")
            weights = parsed.get("weights", {})

            if not llm_code or not llm_code.strip():
                lint_failures = ["LLM returned empty test code"]
                continue
            if not isinstance(weights, dict):
                weights = {}

            llm_code = _sanitize_llm_test_code(llm_code)

            try:
                ast.parse(llm_code)
            except SyntaxError:
                repaired = _auto_repair_truncated_python(llm_code)
                if repaired is not None:
                    _logger.info(
                        "[TASK-TESTGEN] Auto-repaired truncated code on attempt %d for task %s",
                        attempt, task_id,
                    )
                    llm_code = repaired

            clean_weights = {}
            for name, w in weights.items():
                if isinstance(name, str) and isinstance(w, int) and w in _ALLOWED_WEIGHTS:
                    clean_weights[name] = w
            weights = clean_weights or weights

            failures = _self_validate_tests(llm_code, weights, has_api_services=has_api_services, distractor_apis=distractor_apis)

            if not best_code or len(failures) < len(best_failures):
                best_code = llm_code
                best_weights = weights
                best_failures = failures

            if not failures:
                _logger.info(
                    "[TASK-TESTGEN] Passed all lints on attempt %d for task %s", attempt, task_id
                )
                break

            _logger.info(
                "[TASK-TESTGEN] Attempt %d/%d failed %d lints for task %s: %s",
                attempt, MAX_TESTGEN_ATTEMPTS, len(failures), task_id,
                "; ".join(failures[:3]),
            )
            lint_failures = failures

        gen_duration_ms = (time.time() - gen_start) * 1000

        if best_code:
            try:
                ast.parse(best_code)
            except SyntaxError:
                repaired = _auto_repair_truncated_python(best_code)
                if repaired is not None:
                    _logger.warning("[TASK-TESTGEN] Final auto-repair applied for task %s", task_id)
                    best_code = repaired
                else:
                    _logger.error(
                        "[TASK-TESTGEN] Best draft unparseable after auto-repair for task %s; using fallback",
                        task_id,
                    )
                    best_code = _SAFE_FALLBACK_STUB
                    best_weights = {"test_placeholder": 10, "test_placeholder_negative": -10}
        else:
            _logger.error("[TASK-TESTGEN] All attempts produced no code for task %s; using fallback", task_id)
            best_code = _SAFE_FALLBACK_STUB
            best_weights = {"test_placeholder": 10, "test_placeholder_negative": -10}

        full_test_code = wrapper_prefix + best_code
        weights_json = json.dumps(best_weights, indent=2, ensure_ascii=False)

        for write_attempt in range(3):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    task = env["kensei2.kensei2"].browse(task_id)
                    if task.exists():
                        task.write({
                            "test_code": full_test_code,
                            "test_code_status": "done",
                            "test_code_error": False,
                            "test_weights": weights_json,
                            "test_weights_status": "done",
                            "test_weights_error": False,
                        })
                break
            except Exception as e:
                if "serialize" in str(e).lower() and write_attempt < 2:
                    time.sleep(1 + write_attempt)
                    continue
                raise

        _logger.info(
            "[TASK-TESTGEN] Tests generated for task %s: code=%d chars, weights=%d entries, "
            "tokens_in=%d, tokens_out=%d, duration=%.0fms, lint_failures=%d",
            task_id,
            len(full_test_code),
            len(best_weights),
            total_usage.get("input_tokens", 0),
            total_usage.get("output_tokens", 0),
            gen_duration_ms,
            len(best_failures),
        )

    except Exception as e:
        _logger.exception("[TASK-TESTGEN] Test generation failed for task %s", task_id)
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                task = env["kensei2.kensei2"].browse(task_id)
                if task.exists():
                    task.write({
                        "test_code_status": "error",
                        "test_code_error": str(e)[:1000],
                    })
        except Exception:
            _logger.exception("[TASK-TESTGEN] Failed to write error status for task %s", task_id)


def _run_sandbox_start_background(db_name, sandbox_id, mode, notify_partner_id):
    """Background worker: start sandbox (docker compose or K8s), then notify via bus.bus."""
    final_status = "error"
    error_msg = ""
    model_type = ""
    test_gen_thread = None
    try:
        # Phase 1: snapshot what we need (short cursor)
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            sandbox = env["kensei2.sandbox"].browse(sandbox_id)
            if not sandbox.exists():
                _logger.error(
                    "Background sandbox start: sandbox %s does not exist", sandbox_id
                )
                return
            model_type = sandbox.model_type or ""
            task_prompt = ""
            if sandbox.kensei2_id:
                task_prompt = (sandbox.kensei2_id.initial_prompt or "").strip()

        # Phase 1.5: fire intent-based test generation in parallel with pod deploy
        if task_prompt:
            test_gen_thread = threading.Thread(
                target=_generate_intent_tests_background,
                args=(db_name, sandbox_id, task_prompt),
                daemon=True,
            )
            test_gen_thread.start()

        # Phase 2: long-running work (separate cursor per _bg method)
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                sandbox = env["kensei2.sandbox"].browse(sandbox_id)
                if mode == "k8s":
                    sandbox._start_k8s_bg()
                else:
                    sandbox._start_local_bg()
        except Exception as e:
            _logger.exception(
                "Background sandbox start failed for sandbox %s: %s",
                sandbox_id,
                e,
            )
            error_msg = str(e)[:1000]

        # Phase 3: read final status + notify (fresh cursor, retry on conflict)
        for attempt in range(3):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    sandbox = env["kensei2.sandbox"].browse(sandbox_id)
                    if not sandbox.exists():
                        return

                    if error_msg and sandbox.docker_status != "running":
                        sandbox.write(
                            {
                                "docker_status": "error",
                                "docker_error": error_msg,
                            }
                        )

                    final_status = sandbox.docker_status
                    error_msg = sandbox.docker_error or ""

                    partner = None
                    if notify_partner_id:
                        partner = env["res.partner"].browse(notify_partner_id)
                        if not partner.exists():
                            partner = None
                    partners = env["res.partner"]
                    if partner:
                        partners = partner
                    else:
                        for emp in sandbox.employee_ids:
                            p = emp.user_id.partner_id
                            if p:
                                partners |= p
                    for partner in partners:
                        env["bus.bus"]._sendone(
                            partner,
                            "kensei2/sandbox_ready",
                            {
                                "sandbox_id": sandbox_id,
                                "docker_status": final_status,
                                "error": error_msg,
                                "model_type": model_type,
                            },
                        )
                break
            except Exception as e:
                if "serialize" in str(e).lower() and attempt < 2:
                    _logger.warning(
                        "Serialization conflict in Phase 3 for sandbox %s, retry %d",
                        sandbox_id,
                        attempt + 1,
                    )
                    time.sleep(1 + attempt)
                    continue
                raise
    except Exception:
        _logger.exception("Background sandbox start crashed (sandbox=%s)", sandbox_id)
    finally:
        with _SANDBOX_LOCK:
            _SANDBOX_STARTING.discard(sandbox_id)


def _reconcile_one_sandbox(env, sandbox_id, status):
    """Apply a pre-probed k8s status to a single sandbox row.

    `status` is probed by the caller OUTSIDE this transaction (see
    `_cron_reconcile`): the slow k8s API call must not run inside the
    write transaction, or it widens the row-lock window and invites
    SQLSTATE 40001 serialization conflicts against batch-deploy workers.

    Runs inside a fresh per-sandbox transaction so a conflict on this row
    never poisons the reconciliation of its siblings.
    """
    sandbox = env["kensei2.sandbox"].browse(sandbox_id)
    if not sandbox.exists():
        return
    # Re-read inside this transaction — another writer may have moved the
    # row since `status` was probed. Bail if it is no longer ours to
    # reconcile, or already matches the probed status.
    if sandbox.docker_status not in ("starting", "running"):
        return
    if status == sandbox.docker_status:
        return

    update_vals = {"docker_status": status}
    if status == "running" and not sandbox.docker_port:
        update_vals["docker_port"] = 18789
    if status == "error":
        update_vals["docker_error"] = "Sandbox deployment not found after timeout"
    sandbox.write(update_vals)

    if status in ("running", "error"):
        partners = env["res.partner"]
        for emp in sandbox.employee_ids:
            p = emp.user_id.partner_id
            if p:
                partners |= p
        if not partners:
            p = sandbox.kensei2_id.user_id.partner_id
            if p:
                partners = p
        for partner in partners:
            env["bus.bus"]._sendone(
                partner,
                "kensei2/sandbox_ready",
                {
                    "sandbox_id": sandbox.id,
                    "docker_status": status,
                    "error": sandbox.docker_error or "",
                    "model_type": sandbox.model_type,
                },
            )


def _batch_restart_pod(db_name, sandbox_id, mode):
    def _do(env):
        sandbox = env["kensei2.sandbox"].browse(sandbox_id)
        if not sandbox.exists():
            return
        if mode == "k8s":
            sandbox._stop_k8s()
        else:
            sandbox._stop_local()
        sandbox.write({
            "docker_status": "starting",
            "docker_error": False,
        })

    _retry_with_cursor(
        db_name, _do, label="restart_pod sandbox=%s" % sandbox_id
    )


def _batch_is_cancelled(db_name, task_id):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            return task.exists() and task.batch_status in ("stopping", "done", "error")
    except Exception:
        return False


def _batch_ws_health_check(db_name, sandbox_id, retries=3, timeout=15):
    from ..ws_client import OpenClawClient, OpenClawError, OpenClawTimeoutError

    with Registry(db_name).cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        ws_info = env["kensei2.sandbox"].auto_process_get_ws_info(sandbox_id)
        if ws_info.get("error"):
            return False, "WS info error: %s" % ws_info["error"]
        ws_url = ws_info["ws_url"]
        gateway_token = ws_info["gateway_token"]

    for attempt in range(1, retries + 1):
        client = OpenClawClient(ws_url, gateway_token, sandbox_id)
        try:
            client.connect(timeout=timeout)
            client.disconnect()
            _logger.info("[BATCH] WS health check passed for sandbox %s (attempt %d)", sandbox_id, attempt)
            return True, ""
        except (OpenClawError, OpenClawTimeoutError) as e:
            _logger.warning("[BATCH] WS health check %d/%d failed for sandbox %s: %s", attempt, retries, sandbox_id, e)
            try:
                client.disconnect()
            except Exception:
                pass
            if attempt < retries:
                time.sleep(5)

    return False, "WS gateway not reachable after %d health checks" % retries


def _batch_deploy_pod(db_name, sandbox_id, mode, task_id=None):
    max_attempts = _POD_MAX_RETRIES + 1

    for pod_attempt in range(1, max_attempts + 1):
        if task_id and _batch_is_cancelled(db_name, task_id):
            return False, "Batch cancelled", pod_attempt - 1

        if pod_attempt > 1:
            _logger.warning(
                "[BATCH] Pod restart %d/%d for sandbox %s",
                pod_attempt - 1, _POD_MAX_RETRIES, sandbox_id,
            )
            try:
                _batch_restart_pod(db_name, sandbox_id, mode)
            except Exception as e:
                _logger.error("[BATCH] Pod restart failed for sandbox %s: %s", sandbox_id, e)
                return False, "Pod restart failed: %s" % e, pod_attempt - 1
            time.sleep(5)

        _logger.info(
            "[BATCH] Deploying sandbox %s (mode=%s, attempt=%d/%d)",
            sandbox_id, mode, pod_attempt, max_attempts,
        )

        deploy_ok = False
        if mode == "k8s":
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                sandbox = env["kensei2.sandbox"].browse(sandbox_id)
                if not sandbox.exists():
                    return False, "Sandbox %s does not exist" % sandbox_id, pod_attempt - 1
                if not sandbox.docker_gateway_token:
                    sandbox.write({"docker_gateway_token": secrets.token_hex(32)})
                try:
                    env["kensei2.sandbox.k8s"].deploy_sandbox(sandbox)
                    sandbox.write({
                        "docker_compose_project": "kensei2-sandbox-%s" % sandbox_id,
                        "docker_port": 18789,
                    })
                    deploy_ok = True
                except Exception as e:
                    _logger.error("[BATCH] K8s deploy failed for sandbox %s: %s", sandbox_id, e)
                    sandbox.write({"docker_status": "error", "docker_error": str(e)[:1000]})
        else:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                sandbox = env["kensei2.sandbox"].browse(sandbox_id)
                if not sandbox.exists():
                    return False, "Sandbox %s does not exist" % sandbox_id, pod_attempt - 1
                sandbox._start_local_bg()
                deploy_ok = sandbox.docker_status != "error"

        if not deploy_ok:
            if pod_attempt < max_attempts:
                continue
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                sandbox = env["kensei2.sandbox"].browse(sandbox_id)
                return False, "Error: %s" % (sandbox.docker_error or "deploy failed"), pod_attempt - 1

        deadline = time.monotonic() + _BATCH_START_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(10)
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    if task_id:
                        task = env["kensei2.kensei2"].browse(task_id)
                        if task.exists() and task.batch_status in ("stopping", "done", "error"):
                            return False, "Batch cancelled", pod_attempt - 1
                    sandbox = env["kensei2.sandbox"].browse(sandbox_id)
                    if not sandbox.exists():
                        return False, "Sandbox disappeared", pod_attempt - 1
                    if sandbox.docker_status == "running":
                        _logger.info("[BATCH] Sandbox %s already running in DB (attempt %d)", sandbox_id, pod_attempt)
                        return True, "", pod_attempt - 1
                    if sandbox.docker_status == "error":
                        break
                    k8s_status = env["kensei2.sandbox.k8s"].get_sandbox_status(sandbox)
                    if k8s_status == "running":
                        sandbox.write({"docker_status": "running", "docker_port": 18789})
                        _logger.info("[BATCH] Sandbox %s is running (attempt %d)", sandbox_id, pod_attempt)
                        return True, "", pod_attempt - 1
                    if k8s_status == "error":
                        sandbox.write({"docker_status": "error", "docker_error": "K8s deployment failed"})
                        break
            except Exception as poll_err:
                _logger.warning("[BATCH] Poll error for sandbox %s: %s", sandbox_id, poll_err)

        if pod_attempt < max_attempts:
            _logger.warning(
                "[BATCH] Sandbox %s not ready after %ds, retrying (attempt %d/%d)",
                sandbox_id, _BATCH_START_TIMEOUT, pod_attempt, max_attempts,
            )
            continue

    return False, "Pod never became ready after %d attempts" % max_attempts, _POD_MAX_RETRIES


def _batch_run_single_sandbox(db_name, sandbox_id, prompt, mode, attachment_ids=None):
    result = {"sandbox_id": sandbox_id, "status": "error", "error": "", "retries": 0}
    ws_client = None

    try:
        pod_ok, pod_error, retries = _batch_deploy_pod(db_name, sandbox_id, mode)
        result["retries"] = retries

        if not pod_ok:
            result["error"] = pod_error
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    sandbox = env["kensei2.sandbox"].browse(sandbox_id)
                    if sandbox.exists() and sandbox.docker_status not in ("stopped", "error"):
                        sandbox.write({"docker_status": "error", "docker_error": pod_error[:500]})
            except Exception:
                _logger.exception("[BATCH] Failed to mark error for sandbox %s", sandbox_id)
            return result

        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            ws_info = env["kensei2.sandbox"].auto_process_get_ws_info(sandbox_id)
            if ws_info.get("error"):
                result["error"] = "WS info error: %s" % ws_info["error"]
                return result
            ws_url = ws_info["ws_url"]
            gateway_token = ws_info["gateway_token"]

        from ..ws_client import OpenClawClient, OpenClawError, OpenClawTimeoutError

        _logger.info("[BATCH] Connecting WS for sandbox %s: %s", sandbox_id, ws_url)
        ws_client = OpenClawClient(ws_url, gateway_token, sandbox_id)

        for ws_attempt in range(3):
            try:
                ws_client.connect(timeout=30)
                break
            except (OpenClawError, OpenClawTimeoutError) as e:
                if ws_attempt < 2:
                    _logger.warning(
                        "[BATCH] WS connect attempt %d/3 failed for sandbox %s: %s",
                        ws_attempt + 1, sandbox_id, e,
                    )
                    time.sleep(5)
                else:
                    result["error"] = "WS connect failed after 3 attempts: %s" % e
                    return result

        attachments = None
        if attachment_ids:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                attachments = []
                for att in env["ir.attachment"].sudo().browse(attachment_ids):
                    if att.exists() and att.datas:
                        # OpenClaw chat.send → normalizeRpcAttachmentsToChatAttachments
                        # reads { type, mimeType, fileName, content }; content must be a
                        # base64 string. It does not accept `name`/`media` keys.
                        attachments.append({
                            "fileName": att.name,
                            "mimeType": att.mimetype,
                            "content": att.datas.decode(),
                        })
                _logger.info(
                    "[BATCH] Loaded %d attachments for sandbox %s",
                    len(attachments), sandbox_id,
                )

        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            turn_result = env["kensei2.sandbox"].auto_process_create_turn(
                sandbox_id, prompt,
            )
            if turn_result.get("error"):
                result["error"] = "create_turn failed: %s" % turn_result["error"]
                return result
            turn_id = turn_result["turn_id"]

        _logger.info("[BATCH] Sending prompt to sandbox %s (turn=%s, attachments=%d)",
                     sandbox_id, turn_id, len(attachments or []))
        ws_client.send_message(prompt, attachments=attachments)

        response = ws_client.wait_for_response(timeout=600)
        _logger.info(
            "[BATCH] Response received from sandbox %s (%d chars)",
            sandbox_id, len(response.text),
        )

        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["kensei2.sandbox"].auto_process_save_response(
                turn_id, response.text, response.tool_calls_json,
            )

        try:
            history = ws_client.fetch_history(limit=1000)
            if history and isinstance(history, list):
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env["kensei2.sandbox"].auto_process_save_trajectory(
                        sandbox_id, turn_id, history,
                    )
        except Exception as e:
            _logger.warning(
                "[BATCH] Failed to fetch history for sandbox %s: %s", sandbox_id, e,
            )

        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            sandbox = env["kensei2.sandbox"].browse(sandbox_id)
            if sandbox.exists():
                sandbox.write({"session_status": "completed"})

        result["status"] = "completed"
        result["error"] = ""
        _logger.info(
            "[BATCH] Sandbox %s completed successfully (retries=%d)",
            sandbox_id, result["retries"],
        )
        return result

    except Exception as e:
        _logger.exception("[BATCH] Sandbox %s failed: %s", sandbox_id, e)
        result["error"] = str(e)[:2000]
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                sandbox = env["kensei2.sandbox"].browse(sandbox_id)
                if sandbox.exists() and sandbox.docker_status not in ("stopped", "error"):
                    sandbox.write({
                        "docker_status": "error",
                        "docker_error": "Batch execution failed: %s" % str(e)[:500],
                    })
        except Exception:
            _logger.exception("[BATCH] Failed to mark error for sandbox %s", sandbox_id)
        return result

    finally:
        if ws_client:
            try:
                ws_client.disconnect()
            except Exception:
                pass


def _run_batch_background(db_name, task_id, sandbox_ids, prompt, mode, notify_partner_id, attachment_ids=None):
    _logger.info(
        "[BATCH] Starting batch run: task=%s, sandboxes=%d, mode=%s, wave_size=%d, wave_delay=%ds, attachments=%d",
        task_id, len(sandbox_ids), mode, _BATCH_WAVE_SIZE, _BATCH_WAVE_DELAY, len(attachment_ids or []),
    )
    from concurrent.futures import as_completed

    futures = {}
    for i, sid in enumerate(sandbox_ids):
        fut = _BATCH_POOL.submit(_batch_run_single_sandbox, db_name, sid, prompt, mode, attachment_ids)
        futures[fut] = sid
        if _BATCH_WAVE_SIZE > 0 and (i + 1) % _BATCH_WAVE_SIZE == 0 and i + 1 < len(sandbox_ids):
            _logger.info(
                "[BATCH] Wave %d submitted (%d pods), waiting %ds before next wave",
                (i + 1) // _BATCH_WAVE_SIZE, _BATCH_WAVE_SIZE, _BATCH_WAVE_DELAY,
            )
            time.sleep(_BATCH_WAVE_DELAY)

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists():
                task.write({"batch_status": "running"})
                _batch_notify(env, task, "kensei2/batch_status", {
                    "task_id": task_id,
                    "batch_status": "running",
                    "total": len(sandbox_ids),
                })
    except Exception:
        _logger.exception("[BATCH] Failed to set running status for task %s", task_id)

    BATCH_TIMEOUT = int(os.getenv("BATCH_TIMEOUT", "2400"))
    results = {}
    try:
        for fut in as_completed(futures, timeout=BATCH_TIMEOUT):
            sid = futures[fut]
            try:
                results[sid] = fut.result()
            except Exception as e:
                _logger.error("[BATCH] Sandbox %s raised exception: %s", sid, e)
                results[sid] = {"sandbox_id": sid, "status": "error", "error": str(e)[:1000]}
    except TimeoutError:
        _logger.error("[BATCH] Batch timed out after %ds for task %s", BATCH_TIMEOUT, task_id)
        for fut, sid in futures.items():
            if fut.done():
                try:
                    results[sid] = fut.result()
                except Exception as e:
                    results[sid] = {"sandbox_id": sid, "status": "error", "error": str(e)[:500]}
            else:
                results[sid] = {"sandbox_id": sid, "status": "error", "error": "Batch timeout"}
                fut.cancel()

    completed = sum(1 for r in results.values() if r.get("status") == "completed")
    failed = len(results) - completed
    total_retries = sum(r.get("retries", 0) for r in results.values())
    _logger.info(
        "[BATCH] All workers done: task=%s completed=%d failed=%d retries=%d",
        task_id, completed, failed, total_retries,
    )

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists():
                task.write({"batch_status": "stopping"})
                _batch_notify(env, task, "kensei2/batch_status", {
                    "task_id": task_id,
                    "batch_status": "stopping",
                    "completed": completed,
                    "failed": failed,
                })
    except Exception:
        _logger.exception("[BATCH] Failed to set stopping status for task %s", task_id)

    # Export trajectories BEFORE stopping pods — JSONL extraction needs
    # live pods (kubectl exec).  Sequential to avoid concurrent task writes.
    _batch_export_trajectories(db_name, sandbox_ids, "[BATCH]")

    stop_futures = {}
    for i, sid in enumerate(sandbox_ids):
        if i > 0:
            time.sleep(5)
        fut = _BATCH_POOL.submit(_batch_stop_single_sandbox, db_name, sid)
        stop_futures[fut] = sid

    stop_errors = []
    try:
        for fut in as_completed(stop_futures, timeout=300):
            sid = stop_futures[fut]
            try:
                fut.result()
            except Exception as e:
                _logger.error("[BATCH] Stop failed for sandbox %s: %s", sid, e)
                stop_errors.append("sandbox %s: %s" % (sid, str(e)[:200]))
    except TimeoutError:
        _logger.error("[BATCH] Stop phase timed out for task %s", task_id)
        stop_errors.append("Stop phase timed out")

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists():
                final_status = "done" if not failed and not stop_errors else "error"
                error_parts = []
                if failed:
                    error_parts.append("%d sandbox(es) failed." % failed)
                    for sid, r in results.items():
                        if r.get("status") != "completed" and r.get("error"):
                            retries = r.get("retries", 0)
                            retry_note = " (after %d retries)" % retries if retries else ""
                            error_parts.append(
                                "  sandbox %s%s: %s" % (sid, retry_note, r["error"][:300])
                            )
                if stop_errors:
                    error_parts.append("Stop errors: %s" % "; ".join(stop_errors[:5]))
                if total_retries:
                    error_parts.append("Total pod restarts: %d" % total_retries)
                error_msg = "\n".join(error_parts)
                task.write({
                    "batch_status": final_status,
                    "batch_error": error_msg[:4000] if error_msg else False,
                    "batch_completed_at": fields.Datetime.now(),
                })
                _batch_notify(env, task, "kensei2/batch_status", {
                    "task_id": task_id,
                    "batch_status": final_status,
                    "completed": completed,
                    "failed": failed,
                    "retries": total_retries,
                    "error": error_msg[:500] if error_msg else "",
                })
                _logger.info(
                    "[BATCH] Batch finalized: task=%s status=%s completed=%d failed=%d retries=%d",
                    task_id, final_status, completed, failed, total_retries,
                )
    except Exception:
        _logger.exception("[BATCH] Failed to finalize batch status for task %s", task_id)


def _run_batch_deploy_background(db_name, task_id, sandbox_ids, mode, notify_partner_id):
    _logger.info(
        "[BATCH-DEPLOY] Starting deploy: task=%s, sandboxes=%d, mode=%s, wave_size=%d, wave_delay=%ds",
        task_id, len(sandbox_ids), mode, _BATCH_WAVE_SIZE, _BATCH_WAVE_DELAY,
    )
    from concurrent.futures import as_completed

    futures = {}
    cancelled = False
    for i, sid in enumerate(sandbox_ids):
        fut = _BATCH_POOL.submit(_batch_deploy_pod, db_name, sid, mode, task_id)
        futures[fut] = sid
        if _BATCH_WAVE_SIZE > 0 and (i + 1) % _BATCH_WAVE_SIZE == 0 and i + 1 < len(sandbox_ids):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    task = env["kensei2.kensei2"].browse(task_id)
                    if task.exists() and task.batch_status in ("stopping", "done", "error"):
                        _logger.info("[BATCH-DEPLOY] Batch cancelled (status=%s), aborting remaining waves", task.batch_status)
                        cancelled = True
                        break
            except Exception:
                pass
            _logger.info(
                "[BATCH-DEPLOY] Wave %d submitted (%d pods), waiting %ds before next wave",
                (i + 1) // _BATCH_WAVE_SIZE, _BATCH_WAVE_SIZE, _BATCH_WAVE_DELAY,
            )
            time.sleep(_BATCH_WAVE_DELAY)

    if cancelled:
        for fut in futures:
            fut.cancel()
        _logger.info("[BATCH-DEPLOY] Cancelled %d pending deploy futures for task %s", len(futures), task_id)
        return

    BATCH_TIMEOUT = int(os.getenv("BATCH_TIMEOUT", "2400"))
    results = {}
    try:
        for fut in as_completed(futures, timeout=BATCH_TIMEOUT):
            sid = futures[fut]
            try:
                ok, error, retries = fut.result()
                results[sid] = {"ok": ok, "error": error, "retries": retries}
            except Exception as e:
                _logger.error("[BATCH-DEPLOY] Sandbox %s raised exception: %s", sid, e)
                results[sid] = {"ok": False, "error": str(e)[:1000], "retries": 0}
    except TimeoutError:
        _logger.error("[BATCH-DEPLOY] Deploy timed out after %ds for task %s", BATCH_TIMEOUT, task_id)
        for fut, sid in futures.items():
            if fut.done():
                try:
                    ok, error, retries = fut.result()
                    results[sid] = {"ok": ok, "error": error, "retries": retries}
                except Exception as e:
                    results[sid] = {"ok": False, "error": str(e)[:500], "retries": 0}
            else:
                results[sid] = {"ok": False, "error": "Deploy timeout", "retries": 0}
                fut.cancel()

    for sid, r in results.items():
        if not r["ok"]:
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    sandbox = env["kensei2.sandbox"].browse(sid)
                    if sandbox.exists() and sandbox.docker_status not in ("stopped", "error"):
                        sandbox.write({
                            "docker_status": "error",
                            "docker_error": (r["error"] or "deploy failed")[:500],
                        })
            except Exception:
                _logger.exception("[BATCH-DEPLOY] Failed to mark error for sandbox %s", sid)

    deployed = sum(1 for r in results.values() if r["ok"])
    failed = len(results) - deployed
    total_retries = sum(r.get("retries", 0) for r in results.values())

    _logger.info(
        "[BATCH-DEPLOY] All deploys done: task=%s deployed=%d failed=%d retries=%d",
        task_id, deployed, failed, total_retries,
    )

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists():
                if deployed > 0:
                    final_status = "ready"
                    error_parts = []
                    if failed:
                        error_parts.append("%d sandbox(es) failed to deploy." % failed)
                        for sid, r in results.items():
                            if not r["ok"] and r["error"]:
                                retry_note = " (after %d retries)" % r["retries"] if r["retries"] else ""
                                error_parts.append(
                                    "  sandbox %s%s: %s" % (sid, retry_note, r["error"][:300])
                                )
                    if total_retries:
                        error_parts.append("Total pod restarts: %d" % total_retries)
                    error_msg = "\n".join(error_parts)
                else:
                    final_status = "error"
                    error_msg = "All sandboxes failed to deploy."
                    for sid, r in results.items():
                        if r["error"]:
                            error_msg += "\n  sandbox %s: %s" % (sid, r["error"][:300])

                task.write({
                    "batch_status": final_status,
                    "batch_error": error_msg[:4000] if error_msg else False,
                })
                _batch_notify(env, task, "kensei2/batch_status", {
                    "task_id": task_id,
                    "batch_status": final_status,
                    "deployed": deployed,
                    "failed": failed,
                    "retries": total_retries,
                })
                _logger.info(
                    "[BATCH-DEPLOY] Finalized: task=%s status=%s deployed=%d failed=%d",
                    task_id, final_status, deployed, failed,
                )
    except Exception:
        _logger.exception("[BATCH-DEPLOY] Failed to finalize deploy status for task %s", task_id)


def _prepare_batch_attachments(db_name, task_id, attachment_ids):
    """Load ir.attachment bytes, upload them to S3 once (shared by all pods),
    and return parallel lists for the WS payload and the turn.attachments
    JSON. Returns (ws_attachments, persisted_attachments).

    *ws_attachments* — list of {fileName, mimeType, content} dicts for
    OpenClaw chat.send (content is base64 string).
    *persisted_attachments* — list of {name, storedAs, mimeType, size} dicts
    matching the shape read by _build_input_files_manifest and
    _build_multimodal_metadata. The 'storedAs' key is what links each entry
    back to its s3://bucket/prefix/input/tasks/{task_id}/{storedAs} object.
    """
    import uuid as _uuid

    ws_attachments = []
    persisted_attachments = []
    s3_files = []

    if not attachment_ids:
        return ws_attachments, persisted_attachments

    with Registry(db_name).cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        icp = env["ir.config_parameter"].sudo()
        from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX
        bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
        region = icp.get_param("kensei2.s3_region") or "us-east-1"
        prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX
        task = env["kensei2.kensei2"].browse(task_id)
        task_ext_id = (task.task_id or str(task.id)) if task.exists() else str(task_id)

        for att in env["ir.attachment"].sudo().browse(attachment_ids):
            if not (att.exists() and att.datas):
                continue
            name = att.name or ""
            mime = att.mimetype or "application/octet-stream"
            data_b64 = att.datas.decode() if isinstance(att.datas, bytes) else att.datas
            try:
                raw_bytes = base64_mod.b64decode(data_b64)
            except Exception:
                _logger.warning("[BATCH-ATT] Could not decode attachment %s (id=%s)", name, att.id)
                continue
            safe_name = "%s_%s" % (
                _uuid.uuid4().hex[:8],
                name.replace("/", "_").replace("\\", "_"),
            )
            ws_attachments.append({
                "fileName": name,
                "mimeType": mime,
                "content": data_b64,
            })
            persisted_attachments.append({
                "name": name,
                "storedAs": safe_name,
                "mimeType": mime,
                "size": len(raw_bytes),
            })
            s3_files.append({
                "object_key": safe_name,
                "data": raw_bytes,
                "content_type": mime,
            })

    if s3_files and bucket:
        access_key = (
            os.environ.get("KENSEI2_S3_ACCESS_KEY_ID")
            or os.environ.get("AWS_SECRET_KEY", "")
        )
        secret_key = (
            os.environ.get("KENSEI2_S3_SECRET_ACCESS_KEY")
            or os.environ.get("AWS_ACCESS_SECRET_KEY", "")
        )
        try:
            from ..controllers.chat import _upload_to_s3_background
            _upload_to_s3_background(
                bucket, region, prefix, task_ext_id, s3_files,
                subfolder="input",
                access_key=access_key,
                secret_key=secret_key,
            )
        except Exception:
            _logger.exception(
                "[BATCH-ATT] S3 upload failed for task %s (%d files)",
                task_id, len(s3_files),
            )
    elif s3_files:
        _logger.info(
            "[BATCH-ATT] S3 bucket not configured — skipping upload of %d files for task %s",
            len(s3_files), task_id,
        )

    return ws_attachments, persisted_attachments


def _batch_prompt_single_sandbox(db_name, sandbox_id, prompt, attachment_ids=None,
                                  prepared_attachments=None):
    result = {"sandbox_id": sandbox_id, "status": "error", "error": "", "retries": 0}
    ws_client = None

    try:
        from ..ws_client import OpenClawClient, OpenClawError, OpenClawTimeoutError

        attachments = None
        persisted_attachments = None
        if prepared_attachments is not None:
            ws_atts, persisted_attachments = prepared_attachments
            attachments = ws_atts or None
            _logger.info(
                "[BATCH-PROMPT] Using %d pre-prepared attachments for sandbox %s",
                len(attachments or []), sandbox_id,
            )
        elif attachment_ids:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                attachments = []
                for att in env["ir.attachment"].sudo().browse(attachment_ids):
                    if att.exists() and att.datas:
                        attachments.append({
                            "fileName": att.name,
                            "mimeType": att.mimetype,
                            "content": att.datas.decode(),
                        })
                _logger.info(
                    "[BATCH-PROMPT] Loaded %d attachments for sandbox %s (no S3 upload, no turn persistence)",
                    len(attachments), sandbox_id,
                )

        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            turn_result = env["kensei2.sandbox"].auto_process_create_turn(
                sandbox_id, prompt,
            )
            if turn_result.get("error"):
                result["error"] = "create_turn failed: %s" % turn_result["error"]
                return result
            turn_id = turn_result["turn_id"]
            if persisted_attachments:
                turn = env["kensei2.turn"].browse(turn_id)
                if turn.exists():
                    turn.sudo().write({
                        "attachments": json.dumps(persisted_attachments),
                    })

        _WS_MAX_RETRIES = 3
        last_ws_error = None
        for ws_attempt in range(1, _WS_MAX_RETRIES + 1):
            if ws_client:
                try:
                    ws_client.disconnect()
                except Exception:
                    pass
                ws_client = None

            # Re-read WS info from DB each retry (token may have changed)
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                ws_info = env["kensei2.sandbox"].auto_process_get_ws_info(sandbox_id)
                if ws_info.get("error"):
                    last_ws_error = "WS info error: %s" % ws_info["error"]
                    _logger.warning("[BATCH-PROMPT] WS info error on attempt %d/%d for sandbox %s: %s",
                                    ws_attempt, _WS_MAX_RETRIES, sandbox_id, last_ws_error)
                    time.sleep(10)
                    continue
                ws_url = ws_info["ws_url"]
                gateway_token = ws_info["gateway_token"]

            _logger.info("[BATCH-PROMPT] WS attempt %d/%d for sandbox %s: %s",
                         ws_attempt, _WS_MAX_RETRIES, sandbox_id, ws_url)
            ws_client = OpenClawClient(ws_url, gateway_token, sandbox_id)

            try:
                ws_client.connect(timeout=30)
                _logger.info("[BATCH-PROMPT] Sending prompt to sandbox %s (turn=%s, attachments=%d)",
                             sandbox_id, turn_id, len(attachments or []))
                ws_client.send_message(prompt, attachments=attachments)
                response = ws_client.wait_for_response(timeout=600)
                _logger.info(
                    "[BATCH-PROMPT] Response received from sandbox %s (%d chars)",
                    sandbox_id, len(response.text),
                )
                last_ws_error = None
                break
            except (OpenClawError, OpenClawTimeoutError) as e:
                last_ws_error = str(e)
                _logger.warning(
                    "[BATCH-PROMPT] WS attempt %d/%d failed for sandbox %s: %s",
                    ws_attempt, _WS_MAX_RETRIES, sandbox_id, e,
                )
                if ws_attempt < _WS_MAX_RETRIES:
                    time.sleep(10)

        if last_ws_error:
            result["error"] = "WS failed after %d attempts: %s" % (_WS_MAX_RETRIES, last_ws_error)
            return result

        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["kensei2.sandbox"].auto_process_save_response(
                turn_id, response.text, response.tool_calls_json,
            )

        try:
            history = ws_client.fetch_history(limit=1000)
            if history and isinstance(history, list):
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env["kensei2.sandbox"].auto_process_save_trajectory(
                        sandbox_id, turn_id, history,
                    )
        except Exception as e:
            _logger.warning(
                "[BATCH-PROMPT] Failed to fetch history for sandbox %s: %s", sandbox_id, e,
            )

        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            sandbox = env["kensei2.sandbox"].browse(sandbox_id)
            if sandbox.exists():
                sandbox.write({"session_status": "completed"})

        result["status"] = "completed"
        result["error"] = ""
        _logger.info("[BATCH-PROMPT] Sandbox %s completed successfully", sandbox_id)
        return result

    except Exception as e:
        _logger.exception("[BATCH-PROMPT] Sandbox %s failed: %s", sandbox_id, e)
        result["error"] = str(e)[:2000]
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                sandbox = env["kensei2.sandbox"].browse(sandbox_id)
                if sandbox.exists() and sandbox.docker_status not in ("stopped", "error"):
                    sandbox.write({
                        "docker_status": "error",
                        "docker_error": "Batch prompt failed: %s" % str(e)[:500],
                    })
        except Exception:
            _logger.exception("[BATCH-PROMPT] Failed to mark error for sandbox %s", sandbox_id)
        return result

    finally:
        if ws_client:
            try:
                ws_client.disconnect()
            except Exception:
                pass


def _run_batch_prompt_background(db_name, task_id, sandbox_ids, prompt, mode, notify_partner_id, attachment_ids=None):
    _logger.info(
        "[BATCH-PROMPT] Starting prompt phase: task=%s, sandboxes=%d, prompt_len=%d, attachments=%d",
        task_id, len(sandbox_ids), len(prompt), len(attachment_ids or []),
    )
    from concurrent.futures import as_completed

    test_gen_thread = threading.Thread(
        target=_generate_task_tests_background,
        args=(db_name, task_id),
        daemon=True,
    )
    test_gen_thread.start()

    # Upload attachments to S3 once for the whole task and build the WS +
    # turn-persistence payloads so input_files / input_modalities populate.
    prepared_attachments = _prepare_batch_attachments(
        db_name, task_id, attachment_ids,
    ) if attachment_ids else None

    futures = {}
    cancelled = False
    for i, sid in enumerate(sandbox_ids):
        fut = _BATCH_POOL.submit(
            _batch_prompt_single_sandbox,
            db_name, sid, prompt, None, prepared_attachments,
        )
        futures[fut] = sid
        if _BATCH_WAVE_SIZE > 0 and (i + 1) % _BATCH_WAVE_SIZE == 0 and i + 1 < len(sandbox_ids):
            try:
                with Registry(db_name).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    task = env["kensei2.kensei2"].browse(task_id)
                    if task.exists() and task.batch_status in ("stopping", "done", "error"):
                        _logger.info("[BATCH-PROMPT] Batch cancelled (status=%s), aborting remaining waves", task.batch_status)
                        cancelled = True
                        break
            except Exception:
                pass
            _logger.info(
                "[BATCH-PROMPT] Wave %d submitted (%d pods), waiting %ds before next wave",
                (i + 1) // _BATCH_WAVE_SIZE, _BATCH_WAVE_SIZE, _BATCH_WAVE_DELAY,
            )
            time.sleep(_BATCH_WAVE_DELAY)

    if cancelled:
        for fut in futures:
            fut.cancel()
        _logger.info("[BATCH-PROMPT] Cancelled %d pending prompt futures for task %s", len(futures), task_id)
        return

    BATCH_TIMEOUT = int(os.getenv("BATCH_TIMEOUT", "2400"))
    results = {}
    try:
        for fut in as_completed(futures, timeout=BATCH_TIMEOUT):
            sid = futures[fut]
            try:
                results[sid] = fut.result()
            except Exception as e:
                _logger.error("[BATCH-PROMPT] Sandbox %s raised exception: %s", sid, e)
                results[sid] = {"sandbox_id": sid, "status": "error", "error": str(e)[:1000]}
    except TimeoutError:
        _logger.error("[BATCH-PROMPT] Prompt phase timed out after %ds for task %s", BATCH_TIMEOUT, task_id)
        for fut, sid in futures.items():
            if fut.done():
                try:
                    results[sid] = fut.result()
                except Exception as e:
                    results[sid] = {"sandbox_id": sid, "status": "error", "error": str(e)[:500]}
            else:
                results[sid] = {"sandbox_id": sid, "status": "error", "error": "Prompt timeout"}
                fut.cancel()

    completed = sum(1 for r in results.values() if r.get("status") == "completed")
    failed = len(results) - completed
    total_retries = sum(r.get("retries", 0) for r in results.values())
    _logger.info(
        "[BATCH-PROMPT] All workers done: task=%s completed=%d failed=%d",
        task_id, completed, failed,
    )

    test_gen_thread.join(timeout=300)
    if test_gen_thread.is_alive():
        _logger.warning("[BATCH-PROMPT] Test gen thread still running after 300s, proceeding with stop")

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists() and task.test_code_status == "done" and task.test_code:
                TestResult = env["kensei2.test.result"].sudo()
                # Assign 1-based trajectory_index per model type so harbor
                # export ("trajectory_index", ">", 0) includes these records
                # and they map to the correct trajectories/<model>/run_N/.
                model_counters = {}
                created = 0
                for sid in sandbox_ids:
                    sandbox = env["kensei2.sandbox"].browse(sid)
                    if sandbox.exists():
                        mt = sandbox.model_type or "unknown"
                        model_counters[mt] = model_counters.get(mt, 0) + 1
                        TestResult.create({
                            "sandbox_id": sid,
                            "model_used": "task-level",
                            "status": "pending",
                            "test_code": task.test_code,
                            "trajectory_index": model_counters[mt],
                        })
                        created += 1
                _logger.info(
                    "[BATCH-PROMPT] Created %d test.result records from task-level test code "
                    "(per-model counts: %s)",
                    created, model_counters,
                )
    except Exception:
        _logger.exception("[BATCH-PROMPT] Failed to create test.result records for task %s", task_id)

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists():
                task.write({"batch_status": "stopping"})
                _batch_notify(env, task, "kensei2/batch_status", {
                    "task_id": task_id,
                    "batch_status": "stopping",
                    "completed": completed,
                    "failed": failed,
                })
    except Exception:
        _logger.exception("[BATCH-PROMPT] Failed to set stopping status for task %s", task_id)

    # Export trajectories BEFORE stopping pods — JSONL extraction needs
    # live pods (kubectl exec).  Sequential to avoid concurrent task writes.
    _batch_export_trajectories(db_name, sandbox_ids, "[BATCH-PROMPT]")

    stop_futures = {}
    for i, sid in enumerate(sandbox_ids):
        if i > 0:
            time.sleep(5)
        fut = _BATCH_POOL.submit(_batch_stop_single_sandbox, db_name, sid)
        stop_futures[fut] = sid

    stop_errors = []
    try:
        for fut in as_completed(stop_futures, timeout=300):
            sid = stop_futures[fut]
            try:
                fut.result()
            except Exception as e:
                _logger.error("[BATCH-PROMPT] Stop failed for sandbox %s: %s", sid, e)
                stop_errors.append("sandbox %s: %s" % (sid, str(e)[:200]))
    except TimeoutError:
        _logger.error("[BATCH-PROMPT] Stop phase timed out for task %s", task_id)
        stop_errors.append("Stop phase timed out")

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists():
                final_status = "done" if not failed and not stop_errors else "error"
                error_parts = []
                if failed:
                    error_parts.append("%d sandbox(es) failed." % failed)
                    for sid, r in results.items():
                        if r.get("status") != "completed" and r.get("error"):
                            error_parts.append(
                                "  sandbox %s: %s" % (sid, r["error"][:300])
                            )
                if stop_errors:
                    error_parts.append("Stop errors: %s" % "; ".join(stop_errors[:5]))
                error_msg = "\n".join(error_parts)
                task.write({
                    "batch_status": final_status,
                    "batch_error": error_msg[:4000] if error_msg else False,
                    "batch_completed_at": fields.Datetime.now(),
                })
                _batch_notify(env, task, "kensei2/batch_status", {
                    "task_id": task_id,
                    "batch_status": final_status,
                    "completed": completed,
                    "failed": failed,
                    "error": error_msg[:500] if error_msg else "",
                })
                _logger.info(
                    "[BATCH-PROMPT] Finalized: task=%s status=%s completed=%d failed=%d",
                    task_id, final_status, completed, failed,
                )
    except Exception:
        _logger.exception("[BATCH-PROMPT] Failed to finalize batch status for task %s", task_id)


def _run_selective_prompt_background(db_name, task_id, sandbox_ids, prompt,
                                      notify_partner_id, attachment_ids=None):
    """Run a prompt against a user-selected subset of pods, then auto-stop
    them and export trajectories — same finishing flow as the full batch
    prompt, but scoped to the selected pods. Does NOT touch *batch_status*
    of pods that weren't selected.
    """
    _logger.info(
        "[SELECTIVE-PROMPT] Starting: task=%s sandboxes=%d prompt_len=%d attachments=%d",
        task_id, len(sandbox_ids), len(prompt), len(attachment_ids or []),
    )
    from concurrent.futures import as_completed

    # Kick off task-level test code generation in parallel with the prompt
    # phase — mirrors _run_batch_prompt_background. Skip if a prior batch
    # already produced test_code, since selective sends usually run on a
    # task that already has test_code from the initial batch.
    test_gen_thread = None
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            already_have_test_code = bool(
                task.exists()
                and task.test_code
                and task.test_code_status == "done"
            )
    except Exception:
        already_have_test_code = False

    if not already_have_test_code:
        test_gen_thread = threading.Thread(
            target=_generate_task_tests_background,
            args=(db_name, task_id),
            daemon=True,
        )
        test_gen_thread.start()
    else:
        _logger.info(
            "[SELECTIVE-PROMPT] Reusing existing task-level test_code for task %s", task_id,
        )

    # Per-sandbox intent test generation runs during pod deploy in the batch
    # flow. Selective sends reuse already-deployed pods, so we kick the same
    # worker off here in parallel with the prompt phase. Each call creates a
    # test.result with trajectory_index = current_count + 1, so results show
    # inline next to the new trajectory entry the prompt is about to produce.
    intent_test_threads = []
    for sid in sandbox_ids:
        t = threading.Thread(
            target=_generate_intent_tests_background,
            args=(db_name, sid, prompt),
            daemon=True,
        )
        t.start()
        intent_test_threads.append(t)

    # Upload attachments to S3 once for the whole task and build the WS +
    # turn-persistence payloads so input_files / input_modalities populate.
    prepared_attachments = _prepare_batch_attachments(
        db_name, task_id, attachment_ids,
    ) if attachment_ids else None

    futures = {}
    for sid in sandbox_ids:
        fut = _BATCH_POOL.submit(
            _batch_prompt_single_sandbox,
            db_name, sid, prompt, None, prepared_attachments,
        )
        futures[fut] = sid

    BATCH_TIMEOUT = int(os.getenv("BATCH_TIMEOUT", "2400"))
    results = {}
    try:
        for fut in as_completed(futures, timeout=BATCH_TIMEOUT):
            sid = futures[fut]
            try:
                results[sid] = fut.result()
            except Exception as e:
                _logger.error("[SELECTIVE-PROMPT] Sandbox %s raised exception: %s", sid, e)
                results[sid] = {"sandbox_id": sid, "status": "error", "error": str(e)[:1000]}
    except TimeoutError:
        _logger.error("[SELECTIVE-PROMPT] Timed out after %ds for task %s", BATCH_TIMEOUT, task_id)
        for fut, sid in futures.items():
            if fut.done():
                try:
                    results[sid] = fut.result()
                except Exception as e:
                    results[sid] = {"sandbox_id": sid, "status": "error", "error": str(e)[:500]}
            else:
                results[sid] = {"sandbox_id": sid, "status": "error", "error": "Prompt timeout"}
                fut.cancel()

    completed = sum(1 for r in results.values() if r.get("status") == "completed")
    failed = len(results) - completed
    _logger.info(
        "[SELECTIVE-PROMPT] Prompts done: task=%s completed=%d failed=%d — exporting trajectories",
        task_id, completed, failed,
    )

    # Wait for test-gen thread (if we started one) so test_code is ready
    # before we create per-sandbox test.result records.
    if test_gen_thread is not None:
        test_gen_thread.join(timeout=300)
        if test_gen_thread.is_alive():
            _logger.warning(
                "[SELECTIVE-PROMPT] Task-level test gen still running after 300s, proceeding with stop",
            )

    # Wait for per-sandbox intent test generation so the 'pending'
    # test.result records exist before action_stop_sandbox runs
    # _run_pending_tests. If a thread hangs, we proceed without it —
    # _generate_intent_tests handles its own error path.
    for t in intent_test_threads:
        t.join(timeout=300)
        if t.is_alive():
            _logger.warning(
                "[SELECTIVE-PROMPT] Intent test gen thread still running after 300s, proceeding with stop",
            )

    # Create pending test.result records so action_stop_sandbox →
    # _run_pending_tests will execute them in-container before teardown.
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists() and task.test_code_status == "done" and task.test_code:
                TestResult = env["kensei2.test.result"].sudo()
                created = 0
                for sid in sandbox_ids:
                    sandbox = env["kensei2.sandbox"].browse(sid)
                    if sandbox.exists():
                        TestResult.create({
                            "sandbox_id": sid,
                            "model_used": "task-level",
                            "status": "pending",
                            "test_code": task.test_code,
                            "trajectory_index": 0,
                        })
                        created += 1
                _logger.info(
                    "[SELECTIVE-PROMPT] Created %d test.result records from task-level test code",
                    created,
                )
            else:
                _logger.warning(
                    "[SELECTIVE-PROMPT] No test_code available for task %s (status=%s) — skipping test.result creation",
                    task_id,
                    task.test_code_status if task.exists() else "missing",
                )
    except Exception:
        _logger.exception("[SELECTIVE-PROMPT] Failed to create test.result records for task %s", task_id)

    # Export trajectories BEFORE stopping pods — JSONL extraction needs
    # live pods (kubectl exec). Mirrors _run_batch_prompt_background.
    _batch_export_trajectories(db_name, sandbox_ids, "[SELECTIVE-PROMPT]")

    stop_futures = {}
    for i, sid in enumerate(sandbox_ids):
        if i > 0:
            time.sleep(5)
        fut = _BATCH_POOL.submit(_batch_stop_single_sandbox, db_name, sid)
        stop_futures[fut] = sid

    stop_errors = []
    try:
        for fut in as_completed(stop_futures, timeout=300):
            sid = stop_futures[fut]
            try:
                fut.result()
            except Exception as e:
                _logger.error("[SELECTIVE-PROMPT] Stop failed for sandbox %s: %s", sid, e)
                stop_errors.append("sandbox %s: %s" % (sid, str(e)[:200]))
    except TimeoutError:
        _logger.error("[SELECTIVE-PROMPT] Stop phase timed out for task %s", task_id)
        stop_errors.append("Stop phase timed out")

    _logger.info(
        "[SELECTIVE-PROMPT] Finalized: task=%s completed=%d failed=%d stop_errors=%d",
        task_id, completed, failed, len(stop_errors),
    )

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists():
                per_pod = []
                for sid, r in results.items():
                    per_pod.append({
                        "sandbox_id": sid,
                        "status": r.get("status"),
                        "error": (r.get("error") or "")[:300],
                    })
                _batch_notify(env, task, "kensei2/selective_prompt_done", {
                    "task_id": task_id,
                    "sandbox_ids": list(sandbox_ids),
                    "completed": completed,
                    "failed": failed,
                    "stop_errors": stop_errors[:5],
                    "results": per_pod,
                })
    except Exception:
        _logger.exception("[SELECTIVE-PROMPT] Failed to notify selective prompt completion for task %s", task_id)


def _run_batch_stop_background(db_name, task_id, sandbox_ids, notify_partner_id):
    _logger.info(
        "[BATCH-STOP] Starting stop: task=%s, sandboxes=%d",
        task_id, len(sandbox_ids),
    )
    from concurrent.futures import as_completed

    # Export trajectories BEFORE stopping pods — JSONL extraction needs
    # live pods (kubectl exec).  Sequential to avoid concurrent task writes.
    _batch_export_trajectories(db_name, sandbox_ids, "[BATCH-STOP]")

    stop_futures = {}
    for i, sid in enumerate(sandbox_ids):
        if i > 0:
            time.sleep(5)
        fut = _BATCH_POOL.submit(_batch_stop_single_sandbox, db_name, sid)
        stop_futures[fut] = sid

    stop_errors = []
    try:
        for fut in as_completed(stop_futures, timeout=300):
            sid = stop_futures[fut]
            try:
                fut.result()
            except Exception as e:
                _logger.error("[BATCH-STOP] Stop failed for sandbox %s: %s", sid, e)
                stop_errors.append("sandbox %s: %s" % (sid, str(e)[:200]))
    except TimeoutError:
        _logger.error("[BATCH-STOP] Stop phase timed out for task %s", task_id)
        stop_errors.append("Stop phase timed out")
        for fut, sid in stop_futures.items():
            if not fut.done():
                _logger.warning("[BATCH-STOP] Sandbox %s stop timed out, cancelling", sid)
                fut.cancel()

    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            task = env["kensei2.kensei2"].browse(task_id)
            if task.exists():
                status = "done" if not stop_errors else "error"
                task.write({
                    "batch_status": status,
                    "batch_error": "; ".join(stop_errors[:5])[:4000] if stop_errors else False,
                    "batch_completed_at": fields.Datetime.now(),
                })
                _batch_notify(env, task, "kensei2/batch_status", {
                    "task_id": task_id,
                    "batch_status": status,
                    "error": "; ".join(stop_errors[:3])[:500] if stop_errors else "",
                })
                _logger.info(
                    "[BATCH-STOP] Finalized: task=%s status=%s errors=%d",
                    task_id, status, len(stop_errors),
                )
    except Exception:
        _logger.exception("[BATCH-STOP] Failed to finalize batch stop for task %s", task_id)


def _batch_export_trajectories(db_name, sandbox_ids, log_prefix="[BATCH]"):
    for sid in sandbox_ids:
        try:
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                sandbox = env["kensei2.sandbox"].browse(sid)
                if sandbox.exists() and sandbox.kensei2_id:
                    sandbox._export_trajectory_to_task()
                    _logger.info(
                        "%s Trajectory exported for sandbox %s", log_prefix, sid,
                    )
        except Exception:
            _logger.exception(
                "%s Trajectory export failed for sandbox %s", log_prefix, sid,
            )

    # ── Recompute task-level token totals from stored trajectory entries ──
    # Each sandbox's _export_trajectory_to_task() OVERWRITES the task token
    # fields (designed for talos's 1-sandbox-per-model).  With kensei2's
    # 16-sandbox batch, only the LAST sandbox's tokens survive.  Fix: after
    # all exports, sum tokens_in/tokens_out from ALL trajectory entries per
    # model and write the correct totals.
    if not sandbox_ids:
        return
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            sandbox = env["kensei2.sandbox"].browse(sandbox_ids[0])
            if not sandbox.exists() or not sandbox.kensei2_id:
                return
            task = sandbox.kensei2_id

            token_field_map = {
                "claude": ("claude_input_tokens", "claude_output_tokens"),
                "glm": ("glm_input_tokens", "glm_output_tokens"),
                "gpt": ("gpt_input_tokens", "gpt_output_tokens"),
                "1pa": ("onePA_input_tokens", "onePA_output_tokens"),
                "1pb": ("onePB_input_tokens", "onePB_output_tokens"),
                "1pc": ("onePC_input_tokens", "onePC_output_tokens"),
                "1pd": ("onePD_input_tokens", "onePD_output_tokens"),
            }
            token_updates = {}
            for model_type, traj_field in TRAJECTORY_FIELD_MAP.items():
                raw = task[traj_field] or ""
                if not raw.strip():
                    continue
                try:
                    entries = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(entries, list):
                    continue
                total_in = sum(
                    int(e.get("tokens_in", 0) or 0)
                    for e in entries if isinstance(e, dict)
                )
                total_out = sum(
                    int(e.get("tokens_out", 0) or 0)
                    for e in entries if isinstance(e, dict)
                )
                fields_pair = token_field_map.get(model_type)
                if fields_pair:
                    token_updates[fields_pair[0]] = total_in
                    token_updates[fields_pair[1]] = total_out

            if token_updates:
                task.write(token_updates)
                _logger.info(
                    "%s Recomputed token totals for task %s: %s",
                    log_prefix, task.id, token_updates,
                )
    except Exception:
        _logger.exception(
            "%s Token total recomputation failed for sandbox_ids=%s",
            log_prefix, sandbox_ids[:3],
        )


def _batch_stop_single_sandbox(db_name, sandbox_id):
    try:
        with Registry(db_name).cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            sandbox = env["kensei2.sandbox"].browse(sandbox_id)
            if not sandbox.exists():
                _logger.warning("[BATCH] Sandbox %s not found during stop", sandbox_id)
                return
            if sandbox.docker_status == "stopped":
                _logger.info("[BATCH] Sandbox %s already stopped", sandbox_id)
                return
            # Skip trajectory export during batch stop — done sequentially
            # after all pods are stopped to avoid serialization conflicts
            # on the shared task record.
            sandbox.action_stop_sandbox(export_trajectory=False)
            _logger.info("[BATCH] Sandbox %s stopped successfully", sandbox_id)
    except Exception:
        _logger.exception("[BATCH] Stop failed for sandbox %s", sandbox_id)
        raise


def _batch_notify(env, task, channel, payload):
    partners = env["res.partner"]
    for emp in task.employee_ids:
        p = emp.user_id.partner_id
        if p:
            partners |= p
    if not partners and task.user_id:
        partners = task.user_id.partner_id
    for partner in partners:
        env["bus.bus"]._sendone(partner, channel, payload)


_VALID_CHAT_ROLES = {"user", "assistant", "tool", "toolResult", "system"}
_HEARTBEAT_PATTERNS = {"heartbeat_ok", "heartbeat", "pong", "openclaw heartbeat poll"}


def _is_heartbeat_text(text):
    if not text or not isinstance(text, str):
        return False
    lower = text.strip().lower()
    return any(pat in lower for pat in _HEARTBEAT_PATTERNS)


def _extract_message_text(inner):
    content = inner.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    text = inner.get("text")
    if isinstance(text, str):
        return text
    return ""


def _filter_trajectory_messages(messages):
    filtered = []
    dropped = 0
    for msg in messages:
        if isinstance(msg, str):
            dropped += 1
            continue

        if not isinstance(msg, dict):
            dropped += 1
            continue

        inner = msg.get("message", msg) if isinstance(msg.get("message"), dict) else msg
        role = inner.get("role", "")

        if not role or role not in _VALID_CHAT_ROLES:
            dropped += 1
            continue

        if _is_heartbeat_text(_extract_message_text(inner)):
            dropped += 1
            continue

        filtered.append(msg)

    if dropped:
        _logger.info(
            "[BATCH] Filtered %d non-chat messages from trajectory (kept %d)",
            dropped, len(filtered),
        )
    return filtered


def _unwrap_trajectory_messages(messages):
    """Unwrap hint-wrapper format and assign sequential turn_index."""
    unwrapped = []
    for msg in messages:
        if (
            "message" in msg
            and isinstance(msg["message"], dict)
            and "message" in msg["message"]
        ):
            # Wrapped: {"is_accepted": ..., "hints": ..., "message": {actual_msg}}
            actual = msg["message"]
            unwrapped.append(actual)
        else:
            unwrapped.append(msg)
    # Assign turn_index and remove parentId
    for idx, m in enumerate(unwrapped):
        m["turn_index"] = idx
        m.pop("parentId", None)
    return unwrapped


# ---------------------------------------------------------------------------
# Inline media → S3 replacement for trajectory content blocks
# ---------------------------------------------------------------------------

_DATA_URI_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.DOTALL)
_CONTAINER_PATH_RE = re.compile(
    r"^/home/node/\.openclaw/(?:workspace|uploads|media)/(.+)$"
)

_MIME_EXT_MAP = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "image/heic": "heic",
    "image/heif": "heif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/csv": "csv",
    "text/markdown": "md",
    "text/html": "html",
    "application/json": "json",
}

_MEDIA_BLOCK_TYPES = {"image", "video", "audio", "input_image"}


def _s3_https_url(bucket, region, key):
    return "https://%s.s3.%s.amazonaws.com/%s" % (bucket, region, key)


def _upload_bytes_to_s3(bucket, region, prefix, task_id, object_key, data, content_type, access_key, secret_key):
    """Upload raw bytes to S3 synchronously. Returns an HTTPS download URL or None on failure."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig

        client_kwargs = {
            "region_name": region,
            "config": BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
        }
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key

        s3 = boto3.client("s3", **client_kwargs)
        key = "%s/trajectory/tasks/%s/%s" % (prefix, task_id, object_key)
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        url = _s3_https_url(bucket, region, key)
        _logger.info("Trajectory media upload OK: %s (%d bytes)", url, len(data))
        return url
    except Exception:
        _logger.exception(
            "Trajectory media S3 upload failed: bucket=%s task=%s key=%s",
            bucket, task_id, object_key,
        )
        return None


def _replace_inline_media_with_s3(messages, task_id, env):
    """Walk trajectory messages and replace inline base64 / container paths with HTTPS download URLs.

    Mutates messages in-place. Handles wrapped (hint-wrapper) and unwrapped formats.
    Handles three image storage formats:
      1. OpenClaw direct: {"type": "image", "data": "base64...", "mimeType": "image/png"}
      2. Anthropic source dict: {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}
      3. Data URI string: {"type": "image", "source": "data:image/png;base64,..."}
    Also recurses into nested content arrays (tool_result/toolResult blocks).
    """
    from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX

    # Load .env to ensure S3 credentials are available in os.environ
    dotenv = _load_dotenv()

    icp = env["ir.config_parameter"].sudo()
    bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
    region = icp.get_param("kensei2.s3_region") or "us-east-1"
    prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX
    access_key = (
        dotenv.get("KENSEI_S3_ACCESS_KEY_ID", "")
        or os.environ.get("KENSEI_S3_ACCESS_KEY_ID", "")
    )
    secret_key = (
        dotenv.get("KENSEI_S3_SECRET_ACCESS_KEY", "")
        or os.environ.get("KENSEI_S3_SECRET_ACCESS_KEY", "")
    )

    if not bucket:
        _logger.info("S3 bucket not configured — skipping inline media replacement")
        return messages

    replaced_count = 0

    def _process_block(block):
        nonlocal replaced_count
        if not isinstance(block, dict):
            return

        block_type = block.get("type", "")

        # Recurse into tool_result / toolResult nested content
        if block_type in ("tool_result", "toolResult"):
            nested_content = block.get("content")
            if isinstance(nested_content, list):
                for nested_block in nested_content:
                    _process_block(nested_block)
            return

        if block_type not in _MEDIA_BLOCK_TYPES:
            return

        # --- Format 1: OpenClaw direct (data + mimeType on block, no source) ---
        direct_data = block.get("data", "")
        if direct_data and isinstance(direct_data, str) and not block.get("source"):
            mime_type = block.get("mimeType", "") or block.get("media_type", "") or "application/octet-stream"
            try:
                file_bytes = base64_mod.b64decode(direct_data)
            except Exception:
                _logger.warning(
                    "Failed to decode direct base64 data on block (type=%s, task=%s)",
                    block_type, task_id,
                )
                return
            ext = _MIME_EXT_MAP.get(mime_type, mimetypes.guess_extension(mime_type, strict=False) or "bin")
            if ext.startswith("."):
                ext = ext[1:]
            object_key = "%s.%s" % (uuid.uuid4().hex[:12], ext)
            url = _upload_bytes_to_s3(
                bucket, region, prefix, task_id, object_key,
                file_bytes, mime_type, access_key, secret_key,
            )
            if url:
                block.pop("data", None)
                block.pop("mimeType", None)
                block.pop("media_type", None)
                block["source"] = {"type": "url", "url": url}
                replaced_count += 1
            return

        # --- Format 2: Anthropic dict source ---
        source = block.get("source", "")
        if isinstance(source, dict):
            src_type = source.get("type", "")
            b64_data = source.get("data", "")
            mime_type = source.get("media_type", "application/octet-stream")
            if src_type == "base64" and b64_data:
                try:
                    file_bytes = base64_mod.b64decode(b64_data)
                except Exception:
                    _logger.warning(
                        "Failed to decode base64 dict in trajectory block (type=%s, task=%s)",
                        block_type, task_id,
                    )
                    return
                ext = _MIME_EXT_MAP.get(mime_type, mimetypes.guess_extension(mime_type, strict=False) or "bin")
                if ext.startswith("."):
                    ext = ext[1:]
                object_key = "%s.%s" % (uuid.uuid4().hex[:12], ext)
                url = _upload_bytes_to_s3(
                    bucket, region, prefix, task_id, object_key,
                    file_bytes, mime_type, access_key, secret_key,
                )
                if url:
                    block["source"] = {"type": "url", "url": url}
                    replaced_count += 1
            return

        # --- Format 3: String source (data URI or container path) ---
        if not isinstance(source, str) or not source:
            return

        m = _DATA_URI_RE.match(source)
        if m:
            mime_type = m.group(1)
            b64_data = m.group(2)
            try:
                file_bytes = base64_mod.b64decode(b64_data)
            except Exception:
                _logger.warning(
                    "Failed to decode base64 in trajectory block (type=%s, task=%s)",
                    block_type, task_id,
                )
                return
            ext = _MIME_EXT_MAP.get(mime_type, mimetypes.guess_extension(mime_type, strict=False) or "bin")
            if ext.startswith("."):
                ext = ext[1:]
            object_key = "%s.%s" % (uuid.uuid4().hex[:12], ext)
            url = _upload_bytes_to_s3(
                bucket, region, prefix, task_id, object_key,
                file_bytes, mime_type, access_key, secret_key,
            )
            if url:
                block["source"] = url
                replaced_count += 1
            return

        cm = _CONTAINER_PATH_RE.match(source)
        if cm:
            filename = os.path.basename(source)
            output_key = "%s/output/tasks/%s/%s" % (prefix, task_id, filename)
            block["source"] = _s3_https_url(bucket, region, output_key)
            replaced_count += 1

    for msg_wrapper in messages:
        # Navigate into the actual message dict, handling both wrapped and unwrapped formats
        msg = msg_wrapper
        if isinstance(msg, dict) and "message" in msg:
            inner = msg["message"]
            if isinstance(inner, dict) and "message" in inner:
                # Double-wrapped (hint wrapper): {"message": {"message": {..., "content": [...]}}}
                msg = inner["message"]
            elif isinstance(inner, dict) and "content" in inner:
                msg = inner
            elif isinstance(inner, dict) and "role" in inner:
                msg = inner

        if not isinstance(msg, dict):
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            _process_block(block)

    if replaced_count:
        _logger.info(
            "Replaced %d inline media source(s) with HTTPS URLs (task=%s)",
            replaced_count, task_id,
        )
    else:
        _logger.info(
            "No inline media found to replace (task=%s, messages=%d)",
            task_id, len(messages),
        )

    return messages


class Kensei2Sandbox(models.Model):
    _name = "kensei2.sandbox"
    _description = "Kensei2 Sandbox"
    _order = "model_type"

    kensei2_id = fields.Many2one(
        "kensei2.kensei2", required=True, ondelete="cascade", index=True
    )
    employee_id = fields.Many2one(
        related="kensei2_id.employee_id", store=True, readonly=True
    )
    employee_ids = fields.Many2many(
        related="kensei2_id.employee_ids", readonly=True
    )
    model_type = fields.Selection(MODEL_TYPES, required=True, readonly=True)
    variant_index = fields.Integer(
        default=0, readonly=True,
        help="0 = legacy single sandbox, 1-N = batch variant",
    )

    # Docker lifecycle fields (moved from kensei2.kensei2)
    docker_compose_project = fields.Char(readonly=True, copy=False)
    docker_status = fields.Selection(
        [
            ("stopped", "Stopped"),
            ("starting", "Starting"),
            ("running", "Running"),
            ("error", "Error"),
        ],
        default="stopped",
        readonly=True,
    )
    docker_port = fields.Integer(readonly=True)
    docker_litellm_port = fields.Integer(readonly=True)
    docker_gateway_token = fields.Char(readonly=True, copy=False)
    docker_dashboard_url = fields.Char(compute="_compute_dashboard_url")
    docker_ws_url = fields.Char(compute="_compute_docker_ws_url")
    docker_error = fields.Text(readonly=True)
    docker_workdir = fields.Char(readonly=True, copy=False)

    # Session tracking
    session_status = fields.Selection(
        [
            ("not_started", "Not Started"),
            ("in_progress", "In Progress"),
            ("completed", "Completed"),
        ],
        default="not_started",
    )

    # Auto-hint loop state
    auto_hint_status = fields.Selection(
        [
            ("idle", "Idle"),
            ("evaluating", "Evaluating"),
            ("sending_hint", "Sending Hint"),
            ("streaming", "Streaming"),
            ("max_retries", "Max Retries Reached"),
            ("error", "Error"),
        ],
        default="idle",
        help="Current state of the automated hint loop.",
    )
    auto_hint_iteration = fields.Integer(
        string="Auto Hint Current Iteration",
        default=0,
        help="Current iteration count of the in-flight auto-hint loop (0 = idle).",
    )
    auto_hint_group_id = fields.Char(
        string="Auto Hint Group ID",
        help="UUID of the currently active auto-hint loop.",
    )

    # Turns
    turn_ids = fields.One2many("kensei2.turn", "sandbox_id", string="Turns")
    api_request_ids = fields.One2many(
        "kensei2.api.request", "sandbox_id", string="API Request Logs"
    )
    test_result_ids = fields.One2many(
        "kensei2.test.result", "sandbox_id", string="Test Results"
    )

    _sql_constraints = [
        (
            "unique_task_model_variant",
            "UNIQUE(kensei2_id, model_type, variant_index)",
            "Each task can only have one sandbox per model type and variant.",
        ),
    ]

    # ------------------------------------------------------------------
    # Computed fields
    # ------------------------------------------------------------------

    def _deployment_mode(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("kensei2.deployment_mode", "local")
            .strip()
        )

    @api.depends(
        "docker_port", "docker_gateway_token", "docker_status", "docker_compose_project"
    )
    def _compute_dashboard_url(self):
        import urllib.parse

        for rec in self:
            if rec.docker_status != "running" or not rec.docker_gateway_token:
                rec.docker_dashboard_url = False
                continue

            mode = rec._deployment_mode()
            if mode == "k8s":
                ws_host = (
                    rec.env["ir.config_parameter"]
                    .sudo()
                    .get_param("kensei2.ws_router_host", "")
                    .strip()
                )
                if ws_host:
                    # The Control UI cannot derive its own WebSocket URL when
                    # served behind the ws-router prefix: the gateway runs
                    # with no basePath (see kensei2_sandbox_k8s.py). Without
                    # help it dials the wrong target and the dashboard fails
                    # with "disconnected (1006)". Pass the WS endpoint
                    # explicitly via ?gatewayUrl= so the UI dials it verbatim.
                    ws_url = "wss://%s/sandbox/%s/" % (ws_host, rec.id)
                    rec.docker_dashboard_url = (
                        "https://%s/sandbox/%s/?gatewayUrl=%s#token=%s"
                        % (
                            ws_host,
                            rec.id,
                            urllib.parse.quote(ws_url, safe=""),
                            rec.docker_gateway_token,
                        )
                    )
                else:
                    svc_name = "kensei2-sandbox-%s" % rec.id
                    rec.docker_dashboard_url = (
                        "http://%s.kensei2.svc.cluster.local:18789/#token=%s"
                        % (svc_name, rec.docker_gateway_token)
                    )
            else:
                if rec.docker_port:
                    rec.docker_dashboard_url = "http://localhost:%d/#token=%s" % (
                        rec.docker_port,
                        rec.docker_gateway_token,
                    )
                else:
                    rec.docker_dashboard_url = False

    @api.depends("docker_port", "docker_status")
    def _compute_docker_ws_url(self):
        for rec in self:
            if rec.docker_status != "running" or not rec.docker_port:
                rec.docker_ws_url = False
                continue

            mode = rec._deployment_mode()
            if mode == "k8s":
                ws_host = (
                    self.env["ir.config_parameter"]
                    .sudo()
                    .get_param("kensei2.ws_router_host", "")
                    .strip()
                )
                if ws_host:
                    rec.docker_ws_url = "wss://%s/sandbox/%s/" % (ws_host, rec.id)
                else:
                    rec.docker_ws_url = False
            else:
                rec.docker_ws_url = "ws://localhost:%d" % rec.docker_port

    def _get_gateway_ws_url(self):
        self.ensure_one()
        mode = self._deployment_mode()
        if mode == "k8s":
            svc_name = "kensei2-sandbox-%s" % self.id
            return "ws://%s.kensei2.svc.cluster.local:18789" % svc_name
        else:
            if not self.docker_port:
                return False
            return "ws://localhost:%d" % self.docker_port

    # ------------------------------------------------------------------
    # Port allocation
    # ------------------------------------------------------------------

    def _allocate_ports(self):
        self.ensure_one()
        offset = self.id % 5000
        return (
            GATEWAY_PORT_BASE + offset,
            LITELLM_PORT_BASE + offset,
            DB_PORT_BASE + offset,
        )

    # ------------------------------------------------------------------
    # JSONL extraction from OpenClaw container
    # ------------------------------------------------------------------

    def _read_session_jsonl(self):
        self.ensure_one()
        mode = self._deployment_mode()
        if mode == "k8s":
            return self._read_jsonl_k8s()
        return self._read_jsonl_local()

    def _read_jsonl_local(self):
        self.ensure_one()
        workdir = self.docker_workdir
        if not workdir or not os.path.isdir(workdir):
            return []

        persona = self.kensei2_id.persona_id
        persona_name = persona.name if persona else "marcus"
        sessions_dir = os.path.join(
            workdir, "data", persona_name, "agents", "main", "sessions"
        )
        if not os.path.isdir(sessions_dir):
            _logger.warning(
                "Sessions dir not found: %s (sandbox=%s)", sessions_dir, self.id
            )
            return []

        jsonl_files = sorted(
            [f for f in os.listdir(sessions_dir) if f.endswith(".jsonl")],
            key=lambda f: os.path.getmtime(os.path.join(sessions_dir, f)),
        )
        if not jsonl_files:
            _logger.warning("No JSONL files in %s (sandbox=%s)", sessions_dir, self.id)
            return []

        _logger.info(
            "Reading %d JSONL file(s) from %s (sandbox=%s)",
            len(jsonl_files),
            sessions_dir,
            self.id,
        )

        entries = []
        for fname in jsonl_files:
            jsonl_path = os.path.join(sessions_dir, fname)
            with open(jsonl_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    def _read_jsonl_k8s(self):
        self.ensure_one()
        try:
            from kubernetes import client as k8s_client
            from .kensei2_sandbox_k8s import _load_k8s_config

            _load_k8s_config()
        except Exception:
            _logger.warning(
                "K8s not available for JSONL extraction (sandbox=%s)", self.id
            )
            return []

        pod_name = None
        namespace = "default"
        try:
            ns_param = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("kensei2.k8s_namespace", "kensei2")
                .strip()
            )
            if ns_param:
                namespace = ns_param
        except Exception:
            pass

        task_id = self.id
        label_selector = "app.kubernetes.io/name=kensei2-sandbox,task-id=%s" % task_id
        try:
            core_v1 = k8s_client.CoreV1Api()
            pods = core_v1.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )
            for pod in pods.items:
                phase = (pod.status.phase or "").lower()
                if phase not in ("failed", "unknown"):
                    pod_name = pod.metadata.name
                    break
        except Exception as e:
            _logger.warning("Failed to find K8s pod for sandbox %s: %s", self.id, e)
            return []

        if not pod_name:
            _logger.warning("No running pod found for sandbox %s", self.id)
            return []

        try:
            result = subprocess.run(
                [
                    "kubectl",
                    "exec",
                    "-n",
                    namespace,
                    pod_name,
                    "-c",
                    "openclaw",
                    "--",
                    "sh",
                    "-c",
                    "find /home/node/.openclaw -name '*.jsonl' -path '*/sessions/*' 2>/dev/null | xargs cat 2>/dev/null",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                _logger.warning(
                    "kubectl exec returned no data for sandbox %s: %s",
                    self.id,
                    result.stderr[:200],
                )
                return []

            entries = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return entries
        except Exception as e:
            _logger.warning("kubectl exec failed for sandbox %s: %s", self.id, e)
            return []

    # Fields that are internal to OpenClaw and must NOT appear in the
    # delivered trajectory JSON.
    _INTERNAL_MSG_FIELDS = {
        "sender",
        "api",
        "provider",
        "model",
        "usage",
    }
    _INTERNAL_BLOCK_FIELDS = {"api", "provider", "model", "usage"}

    @staticmethod
    def _sanitize_jsonl_message(msg):
        """Strip internal OpenClaw metadata from a JSONL message before export."""
        msg = dict(msg)

        content_before = msg.get("content", [])
        thinking_before = [
            b
            for b in (content_before if isinstance(content_before, list) else [])
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        if thinking_before:
            _logger.info(
                "[THINKING-DEBUG] _sanitize_jsonl_message BEFORE: role=%s thinking_blocks=%d "
                "first_thinking_len=%d has_signature=%s",
                msg.get("role", "?"),
                len(thinking_before),
                len(thinking_before[0].get("thinking", "")),
                bool(thinking_before[0].get("thinkingSignature")),
            )

        for key in Kensei2Sandbox._INTERNAL_MSG_FIELDS:
            msg.pop(key, None)

        content = msg.get("content")
        if isinstance(content, list):
            cleaned = []
            for block in content:
                if isinstance(block, dict):
                    block = dict(block)
                    for key in Kensei2Sandbox._INTERNAL_BLOCK_FIELDS:
                        block.pop(key, None)
                    tcid = block.get("toolCallId", "")
                    if isinstance(tcid, str) and "|" in tcid:
                        block["toolCallId"] = tcid.split("|", 1)[0]
                    tc_id = block.get("id", "")
                    if (
                        block.get("type") == "tool_use"
                        and isinstance(tc_id, str)
                        and "|" in tc_id
                    ):
                        block["id"] = tc_id.split("|", 1)[0]
                cleaned.append(block)
            msg["content"] = cleaned

        thinking_after = [
            b
            for b in (
                msg.get("content", []) if isinstance(msg.get("content"), list) else []
            )
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        if thinking_before and not thinking_after:
            _logger.error(
                "[THINKING-DEBUG] _sanitize_jsonl_message LOST thinking blocks! "
                "before=%d after=%d role=%s",
                len(thinking_before),
                len(thinking_after),
                msg.get("role", "?"),
            )

        return msg

    def _build_trajectory_from_jsonl(self, entries):
        self.ensure_one()
        task = self.kensei2_id

        meta_info = {
            "task_type": self._slugify_task_type(),
            "task_description": task.task_description or "",
            "task_completion_status": "success",
            "system_prompt": task.system_prompt or "",
            "platform": "macOS",
            "multimodal_metadata": self._build_multimodal_metadata(),
            "input_files": self._build_input_files_manifest(),
            "output_artifacts": self._build_output_artifacts(),
        }

        messages = []
        last_kept_id = None
        seen_user_msg = False

        for entry in entries:
            entry_type = entry.get("type", "")
            if entry_type != "message":
                continue

            msg = entry.get("message", {})
            role = msg.get("role", "")
            if not role:
                continue

            if role == "user":
                seen_user_msg = True
            elif role == "system" and not seen_user_msg:
                continue

            msg = self._sanitize_jsonl_message(msg)

            entry_id = entry.get("id", "")
            parent_id = last_kept_id if last_kept_id else entry.get("parentId", "")

            delivery_msg = {
                "type": "message",
                "id": entry_id,
                "parentId": parent_id or "",
                "timestamp": entry.get("timestamp", ""),
                "message": msg,
            }
            messages.append(delivery_msg)
            last_kept_id = entry_id

        all_turns = self.turn_ids.sorted("turn_number")
        if all_turns:
            messages = _wrap_messages_with_turn_feedback(messages, all_turns)
        else:
            messages = [_wrap_trajectory_message(m) for m in messages]

        messages = _unwrap_trajectory_messages(messages)

        task_id = task.task_id or str(task.id)
        messages = _replace_inline_media_with_s3(messages, task_id, self.env)

        return {
            "schema_version": "1.0.0",
            "meta_info": meta_info,
            "messages": messages,
        }

    @staticmethod
    def _extract_tokens_from_jsonl(entries):
        total_in = 0
        total_out = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Usage can live at entry-level or inside entry.message
            usage = entry.get("usage") or {}
            msg = entry.get("message")
            if isinstance(msg, dict):
                usage = usage or msg.get("usage") or {}
            if not usage:
                continue
            if not isinstance(usage, dict):
                continue
            # OpenClaw normalizes to "input"/"output" (bare names).
            # LiteLLM / Anthropic / OpenAI use "_tokens" suffixed variants.
            # Check ALL known field names to cover every provider format.
            raw_in = (
                usage.get("input")              # OpenClaw normalized
                or usage.get("input_tokens")     # Anthropic
                or usage.get("inputTokens")      # camelCase variant
                or usage.get("prompt_tokens")    # OpenAI / LiteLLM
                or 0
            )
            raw_out = (
                usage.get("output")              # OpenClaw normalized
                or usage.get("output_tokens")    # Anthropic
                or usage.get("outputTokens")     # camelCase variant
                or usage.get("completion_tokens") # OpenAI / LiteLLM
                or 0
            )
            total_in += int(raw_in)
            total_out += int(raw_out)
        return total_in, total_out

    def _query_litellm_spend(self, window_start=None, window_end=None):
        self.ensure_one()
        import hashlib
        import urllib.error
        import urllib.parse
        import urllib.request

        mode = self._deployment_mode()
        litellm_key = ""

        if mode == "k8s":
            ws_host = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("kensei2.ws_router_host", "")
                .strip()
            )
            if not ws_host:
                _logger.warning(
                    "No ws_router_host configured, cannot query LiteLLM spend (sandbox=%s)",
                    self.id,
                )
                return 0, 0
            base_url = "https://%s/litellm/%s" % (ws_host, self.id)
            dotenv = _load_dotenv()
            litellm_key = (dotenv.get("KENSEI2_LITELLM_MASTER_KEY") or dotenv.get("LITELLM_MASTER_KEY", "")).strip()
            if not litellm_key:
                litellm_key = (
                    "sk-kensei2-%s" % self.docker_gateway_token[:16]
                    if self.docker_gateway_token
                    else ""
                )
        else:
            litellm_port = self.docker_litellm_port
            if not litellm_port:
                return 0, 0
            base_url = "http://localhost:%d" % litellm_port
            dotenv = _load_dotenv()
            litellm_key = (dotenv.get("KENSEI2_LITELLM_MASTER_KEY") or dotenv.get("LITELLM_MASTER_KEY", "")).strip()
            if not litellm_key and self.docker_gateway_token:
                # Mirror the derivation in _build_compose_env so boot-time and
                # query-time agree when no dotenv key is set.
                litellm_key = "sk-kensei2-%s" % self.docker_gateway_token[:16]

        if not litellm_key:
            _logger.warning(
                "No LITELLM_MASTER_KEY, cannot query LiteLLM spend (sandbox=%s)",
                self.id,
            )
            return 0, 0

        url = base_url
        try:
            if not self.create_date:
                return 0, 0

            # LiteLLM /spend/logs has two response shapes:
            #   * with start_date+end_date -> per-day aggregate (no token fields)
            #   * with api_key (hashed) or no params -> per-request logs
            #     (has prompt_tokens / completion_tokens)
            # We need per-request data, scoped to this sandbox's key, then
            # filter by the sandbox lifetime on the client side.
            hashed_key = hashlib.sha256(litellm_key.encode("utf-8")).hexdigest()
            params = urllib.parse.urlencode({"api_key": hashed_key})
            url = "%s/spend/logs?%s" % (base_url.rstrip("/"), params)

            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": "Bearer %s" % litellm_key,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            logs = data if isinstance(data, list) else data.get("data", [])

            from datetime import datetime as _dt
            from datetime import timezone as _tz

            def _as_utc(dt):
                if dt is None:
                    return None
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=_tz.utc)
                return dt

            start_dt = _as_utc(window_start) or _as_utc(self.create_date)
            end_dt = _as_utc(window_end)

            def _within_window(entry):
                start_time = entry.get("startTime") or entry.get("start_time")
                if not start_time:
                    return True
                try:
                    ts = start_time.replace("Z", "+00:00")
                    entry_dt = _dt.fromisoformat(ts)
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=_tz.utc)
                    if start_dt and entry_dt < start_dt:
                        return False
                    if end_dt and entry_dt > end_dt:
                        return False
                    return True
                except Exception:
                    return True

            total_in = 0
            total_out = 0
            considered = 0
            for entry in logs:
                if not isinstance(entry, dict):
                    continue
                if not _within_window(entry):
                    continue
                considered += 1
                total_in += int(entry.get("prompt_tokens", 0) or 0)
                total_out += int(entry.get("completion_tokens", 0) or 0)

            _logger.info(
                "LiteLLM spend query returned %d logs (%d in window, in=%d, out=%d) for sandbox %s",
                len(logs),
                considered,
                total_in,
                total_out,
                self.id,
            )
            return total_in, total_out

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:500]
            except Exception:
                pass
            _logger.warning(
                "[TOKEN-LITELLM] HTTP %s %s for sandbox %s url=%s body=%s",
                e.code, e.reason, self.id, url, body,
            )
            return 0, 0
        except Exception as e:
            _logger.warning(
                "[TOKEN-LITELLM] Query failed for sandbox %s url=%s: %s",
                self.id, url, e,
            )
            return 0, 0

    def _query_litellm_spend_k8s(self):
        """Query LiteLLM spend directly inside the pod via kubectl exec."""
        self.ensure_one()
        if self._deployment_mode() != "k8s":
            return 0, 0

        import hashlib

        namespace = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("kensei2.k8s_namespace", "kensei2")
            .strip()
        ) or "kensei2"

        dotenv = _load_dotenv()
        litellm_key = (
            dotenv.get("KENSEI2_LITELLM_MASTER_KEY")
            or dotenv.get("LITELLM_MASTER_KEY", "")
        ).strip()
        if not litellm_key:
            litellm_key = (
                "sk-kensei2-%s" % self.docker_gateway_token[:16]
                if self.docker_gateway_token
                else ""
            )
        if not litellm_key:
            return 0, 0

        hashed_key = hashlib.sha256(litellm_key.encode("utf-8")).hexdigest()

        try:
            from kubernetes import client as k8s_client
            from .kensei2_sandbox_k8s import _load_k8s_config

            _load_k8s_config()
        except Exception:
            return 0, 0

        label_selector = (
            "app.kubernetes.io/name=kensei2-sandbox,task-id=%s" % self.id
        )
        try:
            core_v1 = k8s_client.CoreV1Api()
            pods = core_v1.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector,
            )
            pod_name = None
            for pod in pods.items:
                phase = (pod.status.phase or "").lower()
                if phase not in ("failed", "unknown"):
                    pod_name = pod.metadata.name
                    break
            if not pod_name:
                _logger.warning(
                    "[TOKEN-K8S] No pod found for sandbox %s", self.id,
                )
                return 0, 0
        except Exception as e:
            _logger.warning(
                "[TOKEN-K8S] Pod lookup failed for sandbox %s: %s",
                self.id, e,
            )
            return 0, 0

        spend_url = "http://localhost:4000/spend/logs?api_key=%s" % hashed_key
        node_script = (
            "const http=require('http');"
            "const opts={hostname:'localhost',port:4000,"
            "path:'/spend/logs?api_key=%s',"
            "headers:{Authorization:'Bearer %s'}};"
            "http.get(opts,r=>{let d='';r.on('data',c=>d+=c);"
            "r.on('end',()=>{process.stdout.write(d);process.exit(0)})});"
            "setTimeout(()=>process.exit(1),10000)"
        ) % (hashed_key, litellm_key)

        try:
            result = subprocess.run(
                [
                    "kubectl", "exec", "-n", namespace, pod_name,
                    "-c", "openclaw", "--",
                    "node", "-e", node_script,
                ],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                _logger.warning(
                    "[TOKEN-K8S] kubectl exec failed for sandbox %s: rc=%d stderr=%s",
                    self.id, result.returncode, result.stderr[:300],
                )
                return 0, 0

            data = json.loads(result.stdout)
            logs = data if isinstance(data, list) else data.get("data", [])

            total_in = 0
            total_out = 0
            for entry in logs:
                if not isinstance(entry, dict):
                    continue
                total_in += int(entry.get("prompt_tokens", 0) or 0)
                total_out += int(entry.get("completion_tokens", 0) or 0)

            _logger.info(
                "[TOKEN-K8S] Direct query returned %d logs (in=%d, out=%d) sandbox %s",
                len(logs), total_in, total_out, self.id,
            )
            return total_in, total_out
        except Exception as e:
            _logger.warning(
                "[TOKEN-K8S] Direct LiteLLM query failed for sandbox %s: %s",
                self.id, e,
            )
            return 0, 0

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _slugify_task_type(self):
        self.ensure_one()
        task = self.kensei2_id
        l1 = task.l1_classification.name if task.l1_classification else ""
        l2 = task.l2_classification.name if task.l2_classification else ""
        if not l1 and not l2:
            return "uncategorized__uncategorized"
        slug_l1 = re.sub(r"[^a-z0-9]+", "_", (l1 or "uncategorized").lower()).strip("_")
        slug_l2 = re.sub(r"[^a-z0-9]+", "_", (l2 or "uncategorized").lower()).strip("_")
        return "%s__%s" % (slug_l1, slug_l2)

    def _categorize_input_modalities(self, mime_set):
        """Map raw mime strings to coarse modality categories that match
        the cross_modal_reasoning vocabulary. 'text' is implicit (every
        turn has a prompt) so it's added unconditionally."""
        cats = {"text"}
        for mime in mime_set:
            if not mime:
                continue
            if mime.startswith("image/"):
                cats.add("image")
            elif mime.startswith("video/"):
                cats.add("video")
            elif mime.startswith("audio/"):
                cats.add("audio")
            elif mime in (
                "application/pdf", "text/markdown", "text/csv",
                "text/html", "application/json",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ):
                cats.add("document")
            else:
                cats.add("file")
        return sorted(cats)

    def _generate_cross_modal_description(self, modalities_fused):
        """Generate the cross_modal_reasoning.description field by calling
        Bedrock through kensei2's own description generation path
        (generate_task_description_sync → _get_taskdesc_prompt loads
        kensei2/task_description_prompt.md → _call_bedrock_converse with
        kensei2.bedrock_inference_arn). Falls back to a templated string
        when the LLM call is unavailable or fails so the delivery JSON
        always has a usable value."""
        self.ensure_one()
        task = self.kensei2_id
        non_text = [m for m in modalities_fused if m != "text"]
        if not non_text:
            return "Agent processes text inputs only."

        fallback = "Agent processes %s inputs together." % " and ".join(modalities_fused)

        seed_prompt = ""
        if task:
            seed_prompt = (
                task.batch_prompt or task.initial_prompt or task.seed_prompt or ""
            )
        if not seed_prompt:
            return fallback

        messages_payload = []
        for t in self.turn_ids.sorted("turn_number"):
            if t.prompt:
                messages_payload.append({"role": "user", "text": t.prompt})
            if t.response:
                messages_payload.append({"role": "assistant", "text": t.response})

        try:
            from .kensei2 import generate_task_description_sync
            desc, _usage = generate_task_description_sync(
                self.env, seed_prompt, messages_payload,
            )
            desc = (desc or "").strip()
            if desc:
                return desc
        except Exception:
            _logger.exception(
                "[CROSS-MODAL-DESC] LLM generation failed for sandbox %s, using fallback",
                self.id,
            )
        return fallback

    def _scan_task_level_attachments(self, upload_missing_to_s3=True):
        """Walk ir.attachment rows linked to this sandbox's task, returning
        a list of {name, mimeType, size, storedAs} dicts.

        Used as a fallback by _build_input_files_manifest and
        _build_multimodal_metadata when turn.attachments is empty (e.g. for
        tasks prompted before turn.attachments was written, or via flows
        that don't persist to turns).

        When *upload_missing_to_s3* is true and S3 is configured, each
        attachment is also uploaded with a deterministic key
        ``att<id>_<sanitized_name>`` — idempotent, so calling repeatedly is
        safe; the resulting *storedAs* is what the manifest 'source' field
        resolves against.
        """
        self.ensure_one()
        task = self.kensei2_id
        if not task:
            return []

        task_atts = self.env["ir.attachment"].sudo().search([
            ("res_model", "=", task._name),
            ("res_id", "=", task.id),
        ])
        if not task_atts:
            return []

        from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX
        icp = self.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
        region = icp.get_param("kensei2.s3_region") or "us-east-1"
        prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX
        task_ext_id = task.task_id or str(task.id)

        result = []
        s3_files = []
        for att in task_atts:
            if not att.datas:
                continue
            name = att.name or ""
            mime = att.mimetype or "application/octet-stream"
            data_b64 = att.datas.decode() if isinstance(att.datas, bytes) else att.datas
            try:
                raw_bytes = base64_mod.b64decode(data_b64)
            except Exception:
                continue
            safe_name = re.sub(r"[/\\]", "_", name)
            stored_as = "att%d_%s" % (att.id, safe_name)
            result.append({
                "name": name,
                "mimeType": mime,
                "size": len(raw_bytes),
                "storedAs": stored_as,
            })
            if upload_missing_to_s3 and bucket:
                s3_files.append({
                    "object_key": stored_as,
                    "data": raw_bytes,
                    "content_type": mime,
                })

        if s3_files and bucket:
            access_key = (
                os.environ.get("KENSEI2_S3_ACCESS_KEY_ID")
                or os.environ.get("AWS_SECRET_KEY", "")
            )
            secret_key = (
                os.environ.get("KENSEI2_S3_SECRET_ACCESS_KEY")
                or os.environ.get("AWS_ACCESS_SECRET_KEY", "")
            )
            try:
                from ..controllers.chat import _upload_to_s3_background
                _upload_to_s3_background(
                    bucket, region, prefix, task_ext_id, s3_files,
                    subfolder="input",
                    access_key=access_key,
                    secret_key=secret_key,
                )
            except Exception:
                _logger.exception(
                    "[BATCH-ATT] Backfill S3 upload failed for task %s", task.id,
                )

        return result

    def _build_multimodal_metadata(self):
        self.ensure_one()
        task = self.kensei2_id
        modality_tags = set()
        input_modalities = set()

        def _absorb_mime(mime):
            if not mime:
                return
            input_modalities.add(mime)
            if mime.startswith("image/"):
                modality_tags.add("upload_image")
            elif mime == "application/pdf":
                modality_tags.add("pdf")
            elif mime.startswith("video/"):
                modality_tags.add("video")
            elif mime.startswith("audio/"):
                modality_tags.add("audio")

        all_turns = self.turn_ids.sorted("turn_number")
        for t in all_turns:
            if not t.attachments:
                continue
            try:
                atts = json.loads(t.attachments)
                if not isinstance(atts, list):
                    continue
                for att in atts:
                    _absorb_mime(att.get("mimeType", ""))
            except (json.JSONDecodeError, TypeError):
                continue

        # Fallback: cover legacy tasks where turn.attachments was never
        # written. Scans task-level ir.attachment rows so input_modalities
        # populates from the user's original uploads regardless.
        if not input_modalities:
            for att in self._scan_task_level_attachments(upload_missing_to_s3=False):
                _absorb_mime(att.get("mimeType", ""))

        output_modalities = ["text"]
        output_artifacts = self._build_output_artifacts()
        for art in output_artifacts:
            m = art.get("mime_type", "")
            if m.startswith("image/") and "image" not in output_modalities:
                output_modalities.append("image")
            elif m and not m.startswith("image/") and "file" not in output_modalities:
                output_modalities.append("file")

        modalities_fused = self._categorize_input_modalities(input_modalities)
        non_text_modalities = [m for m in modalities_fused if m != "text"]
        cross_modal_pct = 100 if non_text_modalities else 0
        cross_modal_description = self._generate_cross_modal_description(modalities_fused)

        return {
            "modality_tags": sorted(modality_tags),
            "taxonomy_l1": task.l1_classification.name if task.l1_classification else "",
            "taxonomy_l2": task.l2_classification.name if task.l2_classification else "",
            "media_necessity": "Multimodal input required for visual understanding task.",
            "cross_modal_reasoning": {
                "percentage": cross_modal_pct,
                "modalities_fused": modalities_fused,
                "description": cross_modal_description,
            },
            "input_modalities": sorted(input_modalities),
            "output_modalities": output_modalities,
            "asset_realism_notes": "Natural user-uploaded content with realistic filenames and varying quality.",
        }

    def _build_input_files_manifest(self):
        self.ensure_one()
        from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX

        icp = self.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
        prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX
        task_id = self.kensei2_id.task_id or str(self.kensei2_id.id)

        seen_filenames = set()
        manifest = []
        idx = 0
        all_turns = self.turn_ids.sorted("turn_number")
        for t in all_turns:
            if not t.attachments:
                continue
            try:
                atts = json.loads(t.attachments)
                if not isinstance(atts, list):
                    continue
                for att in atts:
                    fname = att.get("name", "")
                    if not fname or fname in seen_filenames:
                        continue
                    seen_filenames.add(fname)
                    mime = att.get("mimeType", "")
                    stored_as = att.get("storedAs", "")
                    entry = {
                        "ref_id": "input_%d" % idx,
                        "filename": fname,
                        "mime_type": mime,
                        "role": "primary_reference",
                        "description": "User-uploaded %s file" % mime,
                        "size_bytes": att.get("size", 0),
                    }
                    if stored_as and bucket:
                        entry["source"] = "s3://%s/%s/input/tasks/%s/%s" % (
                            bucket, prefix, task_id, stored_as
                        )
                    manifest.append(entry)
                    idx += 1
            except (json.JSONDecodeError, TypeError):
                continue

        # Fallback: walk task-level ir.attachment rows for anything not
        # already represented in turn.attachments. Covers legacy tasks
        # prompted before we started persisting attachments to turns.
        for att in self._scan_task_level_attachments():
            fname = att.get("name", "")
            if not fname or fname in seen_filenames:
                continue
            seen_filenames.add(fname)
            mime = att.get("mimeType", "")
            stored_as = att.get("storedAs", "")
            entry = {
                "ref_id": "input_%d" % idx,
                "filename": fname,
                "mime_type": mime,
                "role": "primary_reference",
                "description": "User-uploaded %s file" % mime,
                "size_bytes": att.get("size", 0),
            }
            if stored_as and bucket:
                entry["source"] = "s3://%s/%s/input/tasks/%s/%s" % (
                    bucket, prefix, task_id, stored_as
                )
            manifest.append(entry)
            idx += 1
        return manifest

    def _build_output_artifacts(self):
        self.ensure_one()
        from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX

        icp = self.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
        prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX
        region = icp.get_param("kensei2.s3_region") or "us-east-1"
        task_id = self.kensei2_id.task_id or str(self.kensei2_id.id)

        media_ext_re = re.compile(
            r"/home/node/\.openclaw/(?:workspace|uploads|media)/[^\s\"'`\n)]+\."
            r"(?:png|jpe?g|gif|webp|bmp|svg|heic|heif|mp4|webm|mov|mp3|wav|ogg|m4a|pdf|csv|json|md|txt|html|docx?)",
            re.IGNORECASE,
        )

        mime_map = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
            "svg": "image/svg+xml", "heic": "image/heic", "heif": "image/heif",
            "mp4": "video/mp4", "webm": "video/webm",
            "mov": "video/quicktime", "mp3": "audio/mpeg", "wav": "audio/wav",
            "ogg": "audio/ogg", "m4a": "audio/mp4", "pdf": "application/pdf",
            "csv": "text/csv", "json": "application/json", "md": "text/markdown",
            "txt": "text/plain", "html": "text/html",
            "doc": "application/msword",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }

        write_tool_names = {"write", "save_file", "create_file", "write_file"}
        workspace_prefix = "/home/node/.openclaw/"

        def _classify(ext):
            mime = mime_map.get(ext, "application/octet-stream")
            if mime.startswith("image"):
                return mime, "generated_image"
            if mime.startswith("video") or mime.startswith("audio"):
                return mime, "media"
            if mime == "application/pdf":
                return mime, "document"
            return mime, "data_export"

        def _add_artifact(path, seen, artifacts):
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            if basename in seen:
                return
            seen.add(basename)
            ext = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
            mime, artifact_type = _classify(ext)
            entry = {
                "filename": basename,
                "mime_type": mime,
                "artifact_type": artifact_type,
                "description": "Agent-generated %s output" % ext.upper(),
                "container_path": path if path.startswith(workspace_prefix) else "",
            }
            if bucket:
                entry["source"] = "s3://%s/%s/output/tasks/%s/%s" % (
                    bucket, prefix, task_id, basename
                )
                entry["s3_url"] = "https://%s.s3.%s.amazonaws.com/%s/output/tasks/%s/%s" % (
                    bucket, region, prefix, task_id, basename
                )
            artifacts.append(entry)

        seen = set()
        artifacts = []
        all_turns = self.turn_ids.sorted("turn_number")
        for t in all_turns:
            response_text = t.response or ""
            paths = media_ext_re.findall(response_text)
            for path in paths:
                _add_artifact(path, seen, artifacts)

            tc_raw = t.tool_calls or ""
            if tc_raw:
                try:
                    tc_list = json.loads(tc_raw) if isinstance(tc_raw, str) else tc_raw
                    if isinstance(tc_list, list):
                        for tc in tc_list:
                            if not isinstance(tc, dict):
                                continue
                            name = (tc.get("name") or "").lower()
                            if name in write_tool_names:
                                args = tc.get("args") or tc.get("arguments") or tc.get("input") or {}
                                if isinstance(args, str):
                                    try:
                                        args = json.loads(args)
                                    except (json.JSONDecodeError, TypeError):
                                        args = {}
                                fpath = args.get("path") or args.get("file_path") or args.get("filePath") or ""
                                if fpath and fpath.startswith(workspace_prefix):
                                    _add_artifact(fpath, seen, artifacts)
                            result = tc.get("result") or ""
                            if isinstance(result, str):
                                for rpath in media_ext_re.findall(result):
                                    _add_artifact(rpath, seen, artifacts)
                except (json.JSONDecodeError, TypeError):
                    pass

            traj_raw = t.trajectory_messages or ""
            if traj_raw:
                try:
                    traj_msgs = json.loads(traj_raw) if isinstance(traj_raw, str) else traj_raw
                    if isinstance(traj_msgs, list):
                        for msg in traj_msgs:
                            if not isinstance(msg, dict):
                                continue
                            inner = msg.get("message", msg)
                            if not isinstance(inner, dict):
                                continue
                            role = inner.get("role", "")
                            content = inner.get("content")
                            if role == "assistant" and isinstance(content, list):
                                for block in content:
                                    if not isinstance(block, dict):
                                        continue
                                    if block.get("type") in ("tool_use", "toolCall"):
                                        inp = block.get("input") or block.get("arguments") or {}
                                        if isinstance(inp, str):
                                            try:
                                                inp = json.loads(inp)
                                            except (json.JSONDecodeError, TypeError):
                                                inp = {}
                                        bname = (block.get("name") or "").lower()
                                        if bname in write_tool_names:
                                            fpath = inp.get("path") or inp.get("file_path") or inp.get("filePath") or ""
                                            if fpath and fpath.startswith(workspace_prefix):
                                                _add_artifact(fpath, seen, artifacts)
                            elif role == "tool" or role == "toolResult":
                                text = ""
                                if isinstance(content, str):
                                    text = content
                                elif isinstance(content, list):
                                    text = " ".join(
                                        b.get("text", "") for b in content
                                        if isinstance(b, dict) and b.get("type") == "text"
                                    )
                                for rpath in media_ext_re.findall(text):
                                    _add_artifact(rpath, seen, artifacts)
                except (json.JSONDecodeError, TypeError):
                    pass

        # Fallback: list anything already in s3://<bucket>/<prefix>/output/tasks/<task_id>/
        # and merge it into the manifest. Catches files uploaded by paths the
        # regex didn't recognize (user's custom S3 code, base64-inline media
        # flows, agent writes outside /home/node/.openclaw/, etc.).
        if bucket:
            for s3_entry in self._list_s3_output_objects(
                bucket, region, prefix, task_id,
            ):
                basename = s3_entry["filename"]
                if basename in seen:
                    continue
                seen.add(basename)
                ext = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
                mime, artifact_type = _classify(ext)
                entry = {
                    "filename": basename,
                    "mime_type": mime,
                    "artifact_type": artifact_type,
                    "description": "Agent-generated %s output (from S3)" % (ext.upper() or "binary"),
                    "container_path": "",
                    "source": "s3://%s/%s/output/tasks/%s/%s" % (
                        bucket, prefix, task_id, basename,
                    ),
                    "s3_url": "https://%s.s3.%s.amazonaws.com/%s/output/tasks/%s/%s" % (
                        bucket, region, prefix, task_id, basename,
                    ),
                }
                if s3_entry.get("size") is not None:
                    entry["size_bytes"] = s3_entry["size"]
                artifacts.append(entry)

        return artifacts

    def _list_s3_output_objects(self, bucket, region, prefix, task_id):
        """List existing objects under s3://<bucket>/<prefix>/output/tasks/<task_id>/.

        Returns list of {filename, size}. Empty list on any failure (boto3
        missing, no creds, bucket misconfigured, transient S3 error). Used
        as a fallback by _build_output_artifacts so anything uploaded via
        paths the agent-response regex doesn't catch still surfaces in the
        manifest.
        """
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except Exception:
            return []

        access_key = (
            os.environ.get("KENSEI2_S3_ACCESS_KEY_ID")
            or os.environ.get("AWS_SECRET_KEY", "")
        )
        secret_key = (
            os.environ.get("KENSEI2_S3_SECRET_ACCESS_KEY")
            or os.environ.get("AWS_ACCESS_SECRET_KEY", "")
        )

        client_kwargs = {
            "region_name": region,
            "config": BotoConfig(retries={"max_attempts": 2, "mode": "adaptive"}),
        }
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key

        s3_prefix = "%s/output/tasks/%s/" % (prefix, task_id)
        out = []
        try:
            s3 = boto3.client("s3", **client_kwargs)
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix):
                for obj in page.get("Contents", []) or []:
                    key = obj.get("Key", "")
                    if not key or key.endswith("/"):
                        continue
                    basename = key.rsplit("/", 1)[-1]
                    if not basename:
                        continue
                    out.append({
                        "filename": basename,
                        "size": obj.get("Size"),
                    })
        except Exception:
            _logger.exception(
                "[OUTPUT-ARTIFACTS] S3 list failed for bucket=%s prefix=%s",
                bucket, s3_prefix,
            )
            return []
        _logger.info(
            "[OUTPUT-ARTIFACTS] S3 listing found %d objects under s3://%s/%s",
            len(out), bucket, s3_prefix,
        )
        return out

    def action_export_session(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "/kensei2/chat/export_session?sandbox_id=%d" % self.id,
            "target": "self",
        }

    def build_trajectory_json(self):
        self.ensure_one()
        task = self.kensei2_id
        all_turns = self.turn_ids.sorted("turn_number")

        meta_info = {
            "task_type": self._slugify_task_type(),
            "task_description": task.task_description or "",
            "task_completion_status": "success",
            "system_prompt": task.system_prompt or "",
            "platform": "macOS",
            "multimodal_metadata": self._build_multimodal_metadata(),
            "input_files": self._build_input_files_manifest(),
            "output_artifacts": self._build_output_artifacts(),
        }

        messages = self._trajectory_from_ws()
        if messages:
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s build_trajectory_json: using _trajectory_from_ws path (%d messages)",
                self.id,
                len(messages),
            )
            thinking_count = sum(
                1
                for m in messages
                for b in (m.get("message", m) if isinstance(m, dict) else {}).get(
                    "content", []
                )
                or []
                if isinstance(b, dict) and b.get("type") == "thinking"
            )
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s ws messages thinking_blocks=%d",
                self.id,
                thinking_count,
            )
            messages = _wrap_messages_with_turn_feedback(messages, all_turns)
        else:
            messages = self._trajectory_from_events()
            if messages:
                _logger.info(
                    "[THINKING-DEBUG] sandbox=%s build_trajectory_json: using _trajectory_from_events path (%d messages)",
                    self.id,
                    len(messages),
                )
        if not messages:
            messages = self._trajectory_from_turns()
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s build_trajectory_json: using _trajectory_from_turns path (%d messages)",
                self.id,
                len(messages),
            )

        messages = _unwrap_trajectory_messages(messages)

        task_id = task.task_id or str(task.id)
        messages = _replace_inline_media_with_s3(messages, task_id, self.env)

        return {
            "schema_version": "1.0.0",
            "meta_info": meta_info,
            "messages": messages,
        }

    def _trajectory_from_ws(self):
        self.ensure_one()
        best_messages = []
        best_count = 0
        for t in self.turn_ids.sorted("turn_number", reverse=True):
            if t.trajectory_messages:
                try:
                    ws_messages = json.loads(t.trajectory_messages)
                    if isinstance(ws_messages, list) and ws_messages:
                        thinking_in_turn = sum(
                            1
                            for m in ws_messages
                            for b in (
                                (
                                    m.get("message", m) if isinstance(m, dict) else {}
                                ).get("content", [])
                                or []
                            )
                            if isinstance(b, dict) and b.get("type") == "thinking"
                        )
                        _logger.info(
                            "[THINKING-DEBUG] _trajectory_from_ws: turn=%s turn_number=%s "
                            "messages=%d thinking_blocks=%d",
                            t.id,
                            t.turn_number,
                            len(ws_messages),
                            thinking_in_turn,
                        )
                        if len(ws_messages) > best_count:
                            best_messages = ws_messages
                            best_count = len(ws_messages)
                except (json.JSONDecodeError, TypeError):
                    continue
        if not best_messages:
            _logger.info(
                "[THINKING-DEBUG] _trajectory_from_ws: NO trajectory_messages found "
                "in %d turns (sandbox=%s). Turns with trajectory_messages: %s",
                len(self.turn_ids),
                self.id,
                [t.id for t in self.turn_ids if t.trajectory_messages],
            )
        return best_messages

    def _trajectory_from_events(self):
        self.ensure_one()
        turns = self.turn_ids.sorted("turn_number")
        messages = []
        msg_counter = 0
        parent_id = None

        for t in turns:
            run_id = t.run_id or ""
            user_text = (t.prompt or t.hints or "").strip()
            if t.hints:
                is_accepted = 1
                hints = t.hints.strip()
            else:
                is_accepted = 0
                hints = None

            def _next_id():
                nonlocal msg_counter
                msg_counter += 1
                return "%s:%d" % (run_id, msg_counter) if run_id else ""

            if user_text:
                user_id = _next_id()
                messages.append(
                    {
                        "type": "message",
                        "id": user_id,
                        "parentId": parent_id,
                        "timestamp": t.prompt_timestamp
                        or (t.create_date.isoformat() if t.create_date else ""),
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": user_text}],
                        },
                    }
                )
                parent_id = user_id

            if t.raw_events:
                try:
                    events = json.loads(t.raw_events)
                    if isinstance(events, list) and events:
                        pre_count = len(messages)
                        messages, msg_counter, parent_id = (
                            self.kensei2_id._build_trajectory_from_events(
                                events,
                                messages,
                                msg_counter,
                                parent_id,
                                t.model_name or "",
                            )
                        )
                        for idx in range(pre_count, len(messages)):
                            messages[idx] = _wrap_trajectory_message(
                                messages[idx], is_accepted, hints
                            )
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass

            if t.tool_calls:
                try:
                    calls = json.loads(t.tool_calls)
                    if isinstance(calls, list):
                        for tc in calls:
                            tcid = tc.get("toolCallId", "")
                            call_id = tcid or _next_id()
                            call_msg = {
                                "type": "message",
                                "id": call_id,
                                "parentId": parent_id,
                                "timestamp": t.response_timestamp
                                or (t.write_date.isoformat() if t.write_date else ""),
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "toolCall",
                                            "id": tcid or call_id,
                                            "name": tc.get("name", "unknown"),
                                            "arguments": tc.get("args", {}),
                                        }
                                    ],
                                },
                            }
                            messages.append(
                                _wrap_trajectory_message(call_msg, is_accepted, hints)
                            )
                            parent_id = call_id

                            result_id = ("%s:result" % tcid) if tcid else _next_id()
                            result_text = tc.get("result")
                            if isinstance(result_text, dict):
                                result_text = json.dumps(result_text)
                            elif result_text is None:
                                result_text = ""
                            else:
                                result_text = str(result_text)
                            result_msg = {
                                "type": "message",
                                "id": result_id,
                                "parentId": parent_id,
                                "timestamp": t.response_timestamp
                                or (t.write_date.isoformat() if t.write_date else ""),
                                "message": {
                                    "role": "toolResult",
                                    "toolCallId": tcid or call_id,
                                    "toolName": tc.get("name", "unknown"),
                                    "isError": tc.get("isError", False),
                                    "content": [{"type": "text", "text": result_text}],
                                },
                            }
                            messages.append(
                                _wrap_trajectory_message(result_msg, is_accepted, hints)
                            )
                            parent_id = result_id
                except (json.JSONDecodeError, TypeError):
                    pass

            if t.response:
                asst_id = _next_id()
                asst_msg = {
                    "type": "message",
                    "id": asst_id,
                    "parentId": parent_id,
                    "timestamp": t.response_timestamp
                    or (t.write_date.isoformat() if t.write_date else ""),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": t.response}],
                        "model": t.model_name or "",
                    },
                }
                messages.append(_wrap_trajectory_message(asst_msg, is_accepted, hints))
                parent_id = asst_id

        return messages if messages else []

    def _trajectory_from_turns(self):
        self.ensure_one()
        turns = self.turn_ids.sorted("turn_number")
        messages = []
        msg_counter = 0
        parent_id = None

        for t in turns:
            run_id = t.run_id or ""
            user_text = (t.prompt or t.hints or "").strip()
            if t.hints:
                is_accepted = 1
                hints = t.hints.strip()
            else:
                is_accepted = 0
                hints = None

            def _next_id():
                nonlocal msg_counter
                msg_counter += 1
                return "%s:%d" % (run_id, msg_counter) if run_id else ""

            if user_text:
                user_id = _next_id()
                messages.append(
                    {
                        "type": "message",
                        "id": user_id,
                        "parentId": parent_id,
                        "timestamp": t.prompt_timestamp
                        or (t.create_date.isoformat() if t.create_date else ""),
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": user_text}],
                        },
                    }
                )
                parent_id = user_id

            if t.response:
                asst_id = _next_id()
                asst_msg = {
                    "type": "message",
                    "id": asst_id,
                    "parentId": parent_id,
                    "timestamp": t.response_timestamp
                    or (t.write_date.isoformat() if t.write_date else ""),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": t.response}],
                        "model": t.model_name or "",
                    },
                }
                messages.append(_wrap_trajectory_message(asst_msg, is_accepted, hints))
                parent_id = asst_id

        return messages

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    def action_start_sandbox(self):
        """Start sandbox asynchronously — returns immediately, work runs in background."""
        self.ensure_one()

        if not self.kensei2_id:
            raise UserError(
                "Sandbox is not linked to a task (sandbox_id=%s)." % self.id
            )
        if not self.kensei2_id.persona_id:
            raise UserError(
                "No persona selected on task '%s'. "
                "Please select a persona and save before starting."
                % (self.kensei2_id.display_name or self.kensei2_id.id)
            )
        if self.docker_status in ("starting", "running"):
            raise UserError("Sandbox is already %s." % self.docker_status)

        mode = self._deployment_mode()

        # Pre-validate docker availability (Local mode only)
        if mode != "k8s":
            if not _docker_available():
                raise UserError(
                    "Docker is not available on this server. "
                    "Please ensure the Docker daemon is running."
                )
            if not _compose_cmd():
                raise UserError("docker compose (or docker-compose) not found.")

        # Dedup: prevent duplicate concurrent starts
        with _SANDBOX_LOCK:
            if self.id in _SANDBOX_STARTING:
                raise UserError("Sandbox start is already in progress.")
            _SANDBOX_STARTING.add(self.id)

        # Generate gateway token + allocate ports immediately
        gateway_token = secrets.token_hex(32)
        write_vals = {
            "docker_status": "starting",
            "docker_error": False,
            "docker_gateway_token": gateway_token,
            # Reset auto-hint state from previous sessions
            "auto_hint_status": "idle",
            "auto_hint_iteration": 0,
            "auto_hint_group_id": False,
        }
        if mode != "k8s":
            gateway_port, litellm_port, db_port = self._allocate_ports()
            write_vals["docker_port"] = gateway_port
            write_vals["docker_litellm_port"] = litellm_port
        self.write(write_vals)

        # Capture context for background thread
        sandbox_id = self.id
        db_name = self.env.cr.dbname
        notify_partner_id = self.env.user.partner_id.id

        # Schedule background work AFTER this transaction commits
        @self.env.cr.postcommit.add
        def _queue_sandbox_start():
            _SANDBOX_POOL.submit(
                _run_sandbox_start_background,
                db_name,
                sandbox_id,
                mode,
                notify_partner_id,
            )

        _logger.info(
            "[SANDBOX] action_start_sandbox | sandbox=%s | model=%s | mode=%s | "
            "queued to background pool",
            self.id,
            self.model_type,
            mode,
        )

    def action_retry_pod(self):
        """Re-deploy a single pod that is currently stopped or errored."""
        self.ensure_one()

        if self.docker_status in ("starting", "running"):
            raise UserError(
                "Pod is already %s — nothing to retry." % self.docker_status
            )

        _logger.info(
            "[SANDBOX] action_retry_pod | sandbox=%s | model=%s | "
            "variant_index=%s | current_status=%s",
            self.id,
            self.model_type,
            self.variant_index,
            self.docker_status,
        )

        if self.docker_status == "error":
            mode = self._deployment_mode()
            try:
                if mode == "k8s":
                    self._stop_k8s()
                else:
                    self._stop_local()
            except Exception as e:
                _logger.warning(
                    "action_retry_pod: cleanup failed (sandbox=%s, mode=%s): %s",
                    self.id, mode, e,
                )
                self.write({"docker_status": "stopped", "docker_error": False})

        return self.action_start_sandbox()

    def _persist_output_artifacts_to_s3(self):
        self.ensure_one()
        from .kensei2_sandbox_k8s import S3_BUCKET, S3_KENSEI2_PREFIX, NAMESPACE

        icp = self.env["ir.config_parameter"].sudo()
        bucket = icp.get_param("kensei2.s3_bucket") or S3_BUCKET
        region = icp.get_param("kensei2.s3_region") or "us-east-1"
        prefix = icp.get_param("kensei2.s3_prefix") or S3_KENSEI2_PREFIX
        access_key = (
            os.environ.get("KENSEI2_S3_ACCESS_KEY_ID")
            or os.environ.get("AWS_SECRET_KEY", "")
        )
        secret_key = (
            os.environ.get("KENSEI2_S3_SECRET_ACCESS_KEY")
            or os.environ.get("AWS_ACCESS_SECRET_KEY", "")
        )
        if not bucket:
            _logger.info("_persist_output_artifacts_to_s3: no S3 bucket configured, skipping")
            return

        artifacts = self._build_output_artifacts()
        if not artifacts:
            return

        task_id = self.kensei2_id.task_id or str(self.kensei2_id.id)
        mode = self._deployment_mode()
        persona_name = (
            self.kensei2_id.persona_id.name if self.kensei2_id.persona_id else "marcus"
        )

        s3_files = []
        for art in artifacts:
            container_path = art.get("container_path", "")
            if not container_path:
                continue

            file_bytes = None
            if mode == "k8s":
                file_bytes = self._read_file_from_k8s(container_path, NAMESPACE)
            else:
                file_bytes = self._read_file_from_local(container_path, persona_name)

            if not file_bytes:
                _logger.warning(
                    "_persist_output_artifacts_to_s3: could not read %s (sandbox=%s)",
                    container_path, self.id,
                )
                continue

            s3_files.append({
                "object_key": art["filename"],
                "data": file_bytes,
                "content_type": art.get("mime_type", "application/octet-stream"),
            })

        if not s3_files:
            return

        _logger.info(
            "_persist_output_artifacts_to_s3: uploading %d files for task %s",
            len(s3_files), task_id,
        )
        try:
            import boto3
            from botocore.config import Config as BotoConfig

            client_kwargs = {
                "region_name": region,
                "config": BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
            }
            if access_key and secret_key:
                client_kwargs["aws_access_key_id"] = access_key
                client_kwargs["aws_secret_access_key"] = secret_key

            s3 = boto3.client("s3", **client_kwargs)
            for fm in s3_files:
                key = "%s/output/tasks/%s/%s" % (prefix, task_id, fm["object_key"])
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=fm["data"],
                    ContentType=fm["content_type"],
                )
                _logger.info(
                    "Artifact upload OK: s3://%s/%s (%d bytes)",
                    bucket, key, len(fm["data"]),
                )
        except Exception:
            _logger.exception(
                "_persist_output_artifacts_to_s3 failed for task %s", task_id,
            )

    def _read_file_from_k8s(self, container_path, namespace):
        import subprocess
        import tempfile

        pod_name = "kensei2-sandbox-%s" % self.id
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
        try:
            src = "%s:%s" % (pod_name, container_path)
            subprocess.run(
                ["kubectl", "cp", src, tmp_path, "-n", namespace, "-c", "openclaw"],
                capture_output=True, text=True, timeout=30, check=True,
            )
            with open(tmp_path, "rb") as f:
                return f.read()
        except Exception as e:
            _logger.warning("kubectl cp read failed for %s: %s", container_path, e)
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _read_file_from_local(self, container_path, persona_name):
        workdir = self.docker_workdir
        if not workdir:
            return None
        relative = container_path.replace("/home/node/.openclaw/", "")
        host_path = os.path.realpath(os.path.join(workdir, "data", persona_name, relative))
        allowed_base = os.path.realpath(os.path.join(workdir, "data", persona_name))
        if not host_path.startswith(allowed_base + os.sep):
            _logger.warning("Path traversal blocked: %s", container_path)
            return None
        if not os.path.isfile(host_path):
            return None
        try:
            with open(host_path, "rb") as f:
                return f.read()
        except OSError as e:
            _logger.warning("Failed to read artifact %s: %s", host_path, e)
            return None

    def action_stop_sandbox(self, export_trajectory=True):
        self.ensure_one()

        _logger.info(
            "action_stop_sandbox START (sandbox=%s, status=%s, mode=%s, export_traj=%s)",
            self.id, self.docker_status, self._deployment_mode(), export_trajectory,
        )

        try:
            self._collect_mock_api_audit()
        except Exception as e:
            _logger.warning("API audit collection failed (sandbox=%s): %s", self.id, e)

        _logger.info(
            "action_stop_sandbox: audit done, api_request_ids=%d (sandbox=%s)",
            len(self.api_request_ids), self.id,
        )

        try:
            self._run_pending_tests()
        except Exception as e:
            _logger.warning("Pending test execution failed (sandbox=%s): %s", self.id, e)

        try:
            self._persist_output_artifacts_to_s3()
        except Exception as e:
            _logger.warning("Artifact S3 persistence failed (sandbox=%s): %s", self.id, e)

        if export_trajectory:
            self._export_trajectory_to_task()

        mode = self._deployment_mode()
        if mode == "k8s":
            self._stop_k8s()
        else:
            self._stop_local()

    @staticmethod
    def _count_thinking_blocks(trajectory):
        count = 0
        samples = []
        for msg_envelope in trajectory.get("messages", []):
            inner = msg_envelope
            if isinstance(msg_envelope, dict) and "message" in msg_envelope:
                inner = msg_envelope["message"]
            if not isinstance(inner, dict):
                continue
            content = inner.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    count += 1
                    samples.append(
                        {
                            "thinking_len": len(block.get("thinking", "")),
                            "has_signature": bool(block.get("thinkingSignature")),
                        }
                    )
        return count, samples

    def _export_trajectory_to_task(self):
        self.ensure_one()

        trajectory = None

        jsonl_entries = self._read_session_jsonl()
        if jsonl_entries:
            jsonl_thinking = 0
            for entry in jsonl_entries:
                msg = entry.get("message", {})
                if isinstance(msg, dict):
                    for block in msg.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "thinking":
                            jsonl_thinking += 1
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s JSONL entries=%d thinking_blocks_in_raw_jsonl=%d",
                self.id,
                len(jsonl_entries),
                jsonl_thinking,
            )
            _logger.info(
                "[JSONL-RAW] sandbox=%s entries=%d\n%s",
                self.id,
                len(jsonl_entries),
                json.dumps(jsonl_entries, indent=2, ensure_ascii=False)[:50000],
            )
            trajectory = self._build_trajectory_from_jsonl(jsonl_entries)
            traj_thinking, traj_samples = self._count_thinking_blocks(trajectory)
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s AFTER _build_trajectory_from_jsonl: "
                "thinking_blocks=%d samples=%s",
                self.id,
                traj_thinking,
                traj_samples[:3],
            )
            _logger.info(
                "Built trajectory from JSONL (%d entries, %d messages, sandbox=%s)",
                len(jsonl_entries),
                len(trajectory.get("messages", [])),
                self.id,
            )
        elif self.turn_ids:
            trajectory = self.build_trajectory_json()
            traj_thinking, traj_samples = self._count_thinking_blocks(trajectory)
            _logger.info(
                "[THINKING-DEBUG] sandbox=%s AFTER build_trajectory_json (turns fallback): "
                "thinking_blocks=%d samples=%s",
                self.id,
                traj_thinking,
                traj_samples[:3],
            )
            _logger.info(
                "Built trajectory from turns fallback (%d messages, sandbox=%s)",
                len(trajectory.get("messages", [])),
                self.id,
            )

        if trajectory:
            field_name = TRAJECTORY_FIELD_MAP.get(self.model_type)
            if field_name and self.kensei2_id:
                from datetime import datetime as _dt
                from datetime import timezone as _tz

                # Replace-on-stop semantics: each sandbox stop for this model
                # REPLACES any previously stored trajectory. One trajectory
                # per model, always the latest. Token spend window therefore
                # spans this sandbox's full lifetime (create_date -> now).
                window_end = _dt.now(_tz.utc)

                session_in, session_out = 0, 0
                source = "none"
                if self.model_type in ("claude", "glm", "gpt", "1pa", "1pb", "1pc", "1pd"):
                    session_in, session_out = self._query_litellm_spend(
                        window_start=None, window_end=window_end
                    )
                    source = "litellm"
                    if session_in == 0 and session_out == 0:
                        session_in, session_out = self._query_litellm_spend_k8s()
                        source = "litellm-k8s"
                    if session_in == 0 and session_out == 0 and jsonl_entries:
                        session_in, session_out = self._extract_tokens_from_jsonl(
                            jsonl_entries
                        )
                        source = "jsonl"

                session_entry = {
                    "session_id": secrets.token_hex(8),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "trajectory": trajectory,
                    "tokens_in": session_in,
                    "tokens_out": session_out,
                    "token_source": source,
                    "window_end": window_end.isoformat(),
                }

                # APPEND to existing trajectory entries (multi-session, cap at 12)
                MAX_TRAJECTORIES_PER_MODEL = 12
                existing_raw = self.kensei2_id[field_name] or ""
                entries = json.loads(existing_raw) if existing_raw.strip() else []
                if not isinstance(entries, list):
                    entries = []
                entries.append(session_entry)
                # Cap at 12 — keep most recent
                if len(entries) > MAX_TRAJECTORIES_PER_MODEL:
                    entries = entries[-MAX_TRAJECTORIES_PER_MODEL:]
                new_value = json.dumps(entries, indent=2, ensure_ascii=False)

                self.kensei2_id.write({field_name: new_value})
                _logger.info(
                    "Stored trajectory session %s (tokens_in=%d, tokens_out=%d, source=%s) to %s for task %s",
                    session_entry["session_id"],
                    session_in,
                    session_out,
                    source,
                    field_name,
                    self.kensei2_id.id,
                )

                token_field_map = {
                    "claude": ("claude_input_tokens", "claude_output_tokens"),
                    "glm": ("glm_input_tokens", "glm_output_tokens"),
                    "gpt": ("gpt_input_tokens", "gpt_output_tokens"),
                    "1pa": ("onePA_input_tokens", "onePA_output_tokens"),
                    "1pb": ("onePB_input_tokens", "onePB_output_tokens"),
                    "1pc": ("onePC_input_tokens", "onePC_output_tokens"),
                    "1pd": ("onePD_input_tokens", "onePD_output_tokens"),
                }
                fields_pair = token_field_map.get(self.model_type)
                if fields_pair:
                    self.kensei2_id.write(
                        {
                            fields_pair[0]: session_in,
                            fields_pair[1]: session_out,
                        }
                    )
                    _logger.info(
                        "Saved token usage (in=%d, out=%d) to %s/%s for task %s",
                        session_in,
                        session_out,
                        fields_pair[0],
                        fields_pair[1],
                        self.kensei2_id.id,
                    )


        if self.turn_ids:
            # Aggregate bedrock QC tokens to task level before deleting turns
            if self.kensei2_id:
                bedrock_in = sum(t.bedrock_input_tokens or 0 for t in self.turn_ids)
                bedrock_out = sum(t.bedrock_output_tokens or 0 for t in self.turn_ids)
                if bedrock_in > 0 or bedrock_out > 0:
                    self.kensei2_id.write(
                        {
                            "bedrock_input_tokens": (
                                self.kensei2_id.bedrock_input_tokens or 0
                            )
                            + bedrock_in,
                            "bedrock_output_tokens": (
                                self.kensei2_id.bedrock_output_tokens or 0
                            )
                            + bedrock_out,
                        }
                    )
                    _logger.info(
                        "Aggregated bedrock QC tokens (in=%d, out=%d) to task %s",
                        bedrock_in,
                        bedrock_out,
                        self.kensei2_id.id,
                    )

                turn_token_map = {
                    "claude": (
                        "claude_input_tokens",
                        "claude_output_tokens",
                        "claude_input_tokens",
                        "claude_output_tokens",
                    ),
                    "glm": (
                        "glm_input_tokens",
                        "glm_output_tokens",
                        "glm_input_tokens",
                        "glm_output_tokens",
                    ),
                    "gpt": (
                        "gpt_input_tokens",
                        "gpt_output_tokens",
                        "gpt_input_tokens",
                        "gpt_output_tokens",
                    ),
                }
                turn_fields = turn_token_map.get(self.model_type)
                if turn_fields:
                    turn_in_field, turn_out_field, task_in_field, task_out_field = (
                        turn_fields
                    )
                    t_in = sum(getattr(t, turn_in_field, 0) or 0 for t in self.turn_ids)
                    t_out = sum(
                        getattr(t, turn_out_field, 0) or 0 for t in self.turn_ids
                    )
                    if t_in > 0 or t_out > 0:
                        existing_in = getattr(self.kensei2_id, task_in_field, 0) or 0
                        existing_out = getattr(self.kensei2_id, task_out_field, 0) or 0
                        self.kensei2_id.write(
                            {
                                task_in_field: existing_in + t_in,
                                task_out_field: existing_out + t_out,
                            }
                        )
                        _logger.info(
                            "Aggregated %s turn tokens (in=%d, out=%d) to task %s",
                            self.model_type,
                            t_in,
                            t_out,
                            self.kensei2_id.id,
                        )

            turn_count = len(self.turn_ids)
            self.turn_ids.unlink()
            _logger.info(
                "Cleared %d turns for sandbox %s (session isolation)",
                turn_count,
                self.id,
            )

    def _start_k8s(self):
        if self.docker_status == "running":
            raise UserError("Sandbox is already running.")

        gateway_token = secrets.token_hex(32)
        self.write(
            {
                "docker_status": "starting",
                "docker_gateway_token": gateway_token,
                "docker_error": False,
            }
        )

        try:
            self.env["kensei2.sandbox.k8s"].deploy_sandbox(self)
            svc_name = "kensei2-sandbox-%s" % self.id
            self.write(
                {
                    "docker_compose_project": svc_name,
                    "docker_status": "starting",
                    "docker_port": 18789,
                }
            )
            _logger.info(
                "Deployed K8s sandbox %s for sandbox %s (persona=%s, model=%s)",
                svc_name,
                self.id,
                self.kensei2_id.persona_id.name,
                self.model_type,
            )
        except Exception as e:
            _logger.error("K8s sandbox deploy failed for sandbox %s: %s", self.id, e)
            self.write({"docker_status": "error", "docker_error": str(e)[:1000]})

    def _stop_k8s(self):
        if self.docker_status == "stopped":
            return

        try:
            self.env["kensei2.sandbox.k8s"].destroy_sandbox(self)
            _logger.info("Destroyed K8s sandbox for sandbox %s", self.id)
        except Exception as e:
            _logger.warning("K8s sandbox destroy failed for sandbox %s: %s", self.id, e)

        self.write(
            {
                "docker_compose_project": False,
                "docker_status": "stopped",
                "docker_port": 0,
                "docker_litellm_port": 0,
                "docker_gateway_token": False,
                "docker_error": False,
            }
        )

    def _start_local(self):
        if self.docker_status == "running" and self.docker_compose_project:
            raise UserError("Docker stack is already running for this sandbox.")

        if not _docker_available():
            raise UserError(
                "Docker is not available on this server. "
                "Please ensure the Docker daemon is running."
            )

        compose_bin = _compose_cmd()
        if not compose_bin:
            raise UserError("docker compose (or docker-compose) not found.")

        persona = self.kensei2_id.persona_id
        if not persona:
            raise UserError(
                "No persona selected for the parent task (task_id=%s, sandbox_id=%s, kensei2_id=%s)."
                % (self.kensei2_id.id, self.id, self.kensei2_id)
            )

        gateway_token = secrets.token_hex(32)
        project_name = "kensei2-%d-%s" % (self.kensei2_id.id, self.model_type)
        gateway_port, litellm_port, db_port = self._allocate_ports()

        try:
            workdir = self._prepare_workdir(
                persona, gateway_token, gateway_port, litellm_port, db_port
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "Failed to prepare sandbox: %s" % str(e)[:500],
                }
            )
            return

        compose_env = self._build_compose_env(gateway_token)

        self.write(
            {
                "docker_compose_project": project_name,
                "docker_status": "starting",
                "docker_port": gateway_port,
                "docker_litellm_port": litellm_port,
                "docker_gateway_token": gateway_token,
                "docker_workdir": workdir,
                "docker_error": False,
            }
        )
        self.env.cr.commit()

        cmd = compose_bin + [
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.override.yml",
            "-p",
            project_name,
            "up",
            "-d",
            "--build",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
                cwd=workdir,
                env=compose_env,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": "Compose up failed (exit %d): %s"
                        % (result.returncode, error_msg[:1000]),
                    }
                )
                return

            healthy = self._wait_for_health(compose_bin, project_name, workdir)

            if healthy:
                self.write({"docker_status": "running"})
                _logger.info(
                    "Started sandbox (project=%s) sandbox=%s persona=%s model=%s",
                    project_name,
                    self.id,
                    persona.name,
                    self.model_type,
                )
            else:
                logs = self._capture_container_logs(compose_bin, project_name, workdir)
                error_detail = (
                    "Sandbox containers started but the gateway never became "
                    "healthy within %d seconds." % _HEALTH_WAIT_TIMEOUT
                )
                if logs:
                    error_detail += (
                        "\n\nContainer logs (last 30 lines):\n%s" % logs[:2000]
                    )
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": error_detail[:4000],
                    }
                )
                _logger.error(
                    "Gateway health-check failed for project %s (sandbox %s)",
                    project_name,
                    self.id,
                )

        except subprocess.TimeoutExpired:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "docker compose up timed out after 900 seconds",
                }
            )
        except Exception as e:
            self.write({"docker_status": "error", "docker_error": str(e)[:500]})

    def _start_local_bg(self):
        """Start local Docker sandbox — called from background thread."""
        compose_bin = _compose_cmd()
        persona = self.kensei2_id.persona_id
        gateway_token = self.docker_gateway_token
        if not gateway_token:
            _logger.warning(
                "[SANDBOX] _start_local_bg: docker_gateway_token is empty for "
                "sandbox %s, regenerating",
                self.id,
            )
            gateway_token = secrets.token_hex(32)
            self.write({"docker_gateway_token": gateway_token})
        gateway_port = self.docker_port
        litellm_port = self.docker_litellm_port
        db_port = DB_PORT_BASE + (self.id % 5000)
        project_name = "kensei2-%d-%s" % (self.kensei2_id.id, self.model_type)

        try:
            workdir = self._prepare_workdir(
                persona, gateway_token, gateway_port, litellm_port, db_port
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "Failed to prepare sandbox: %s" % str(e)[:500],
                }
            )
            return

        compose_env = self._build_compose_env(gateway_token)

        cmd = compose_bin + [
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.override.yml",
            "-p",
            project_name,
            "up",
            "-d",
            "--build",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
                cwd=workdir,
                env=compose_env,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": "Compose up failed: %s" % error_msg[:1000],
                    }
                )
                return

            self.write(
                {
                    "docker_compose_project": project_name,
                    "docker_workdir": workdir,
                }
            )

            healthy = self._wait_for_health(compose_bin, project_name, workdir)

            if healthy:
                self.write({"docker_status": "running"})
                _logger.info(
                    "Started sandbox (project=%s) sandbox=%s persona=%s model=%s",
                    project_name,
                    self.id,
                    persona.name,
                    self.model_type,
                )
            else:
                logs = self._capture_container_logs(compose_bin, project_name, workdir)
                error_detail = (
                    "Sandbox containers started but the gateway never became "
                    "healthy within %d seconds." % _HEALTH_WAIT_TIMEOUT
                )
                if logs:
                    error_detail += (
                        "\n\nContainer logs (last 30 lines):\n%s" % logs[:2000]
                    )
                self.write(
                    {
                        "docker_status": "error",
                        "docker_error": error_detail[:4000],
                    }
                )

        except subprocess.TimeoutExpired:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": "docker compose up timed out after 900 seconds",
                }
            )
        except Exception as e:
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": str(e)[:500],
                }
            )

    def _start_k8s_bg(self, start_timeout=300):
        if not self.docker_gateway_token:
            _logger.warning(
                "[SANDBOX] _start_k8s_bg: docker_gateway_token is empty for "
                "sandbox %s, regenerating",
                self.id,
            )
            self.write({"docker_gateway_token": secrets.token_hex(32)})
        try:
            self.env["kensei2.sandbox.k8s"].deploy_sandbox(self)
            svc_name = "kensei2-sandbox-%s" % self.id
            self.write(
                {
                    "docker_compose_project": svc_name,
                    "docker_port": 18789,
                }
            )
            _logger.info(
                "Deployed K8s sandbox %s for sandbox %s (persona=%s, model=%s)",
                svc_name,
                self.id,
                self.kensei2_id.persona_id.name,
                self.model_type,
            )
        except Exception as e:
            _logger.error("K8s sandbox deploy failed for sandbox %s: %s", self.id, e)
            self.write(
                {
                    "docker_status": "error",
                    "docker_error": str(e)[:1000],
                }
            )
            return

        k8s_model = self.env["kensei2.sandbox.k8s"]
        deadline = time.monotonic() + start_timeout
        while time.monotonic() < deadline:
            try:
                status = k8s_model.get_sandbox_status(self)
                if status == "running":
                    update_vals = {"docker_status": "running"}
                    if not self.docker_port:
                        update_vals["docker_port"] = 18789
                    self.write(update_vals)
                    _logger.info(
                        "K8s sandbox %s is now running",
                        self.id,
                    )
                    return
                if status == "error":
                    self.write(
                        {
                            "docker_status": "error",
                            "docker_error": "K8s deployment failed",
                        }
                    )
                    return
            except Exception as e:
                _logger.debug("K8s readiness poll error: %s", e)
            time.sleep(5)

        _logger.warning(
            "K8s sandbox %s did not become ready within %ds",
            self.id,
            start_timeout,
        )

    def _stop_local(self):
        if self.docker_status == "stopped":
            return

        compose_bin = _compose_cmd()
        project_name = self.docker_compose_project
        workdir = self.docker_workdir

        if compose_bin and project_name and workdir and os.path.isdir(workdir):
            try:
                cmd = compose_bin + ["-p", project_name]
                cmd += ["-f", "docker-compose.yml"]
                override = os.path.join(workdir, "docker-compose.override.yml")
                if os.path.isfile(override):
                    cmd += ["-f", "docker-compose.override.yml"]
                cmd += ["down", "--volumes", "--remove-orphans"]

                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    cwd=workdir,
                )
                _logger.info(
                    "Stopped sandbox (project=%s) sandbox=%s",
                    project_name,
                    self.id,
                )
            except Exception as e:
                _logger.warning(
                    "Failed to stop compose project %s: %s", project_name, e
                )
        elif compose_bin and project_name:
            try:
                subprocess.run(
                    compose_bin
                    + [
                        "-p",
                        project_name,
                        "down",
                        "--volumes",
                        "--remove-orphans",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except Exception as e:
                _logger.warning("Force stop failed: %s", e)

        if workdir and os.path.isdir(workdir):
            try:
                shutil.rmtree(workdir)
            except Exception as e:
                _logger.warning("Could not clean workdir %s: %s", workdir, e)

        self.write(
            {
                "docker_compose_project": False,
                "docker_status": "stopped",
                "docker_port": 0,
                "docker_litellm_port": 0,
                "docker_gateway_token": False,
                "docker_workdir": False,
                "docker_error": False,
            }
        )

    # ------------------------------------------------------------------
    # Helper methods for local lifecycle
    # ------------------------------------------------------------------

    def _prepare_workdir(
        self, persona, gateway_token, gateway_port, litellm_port, db_port
    ):
        env = _load_dotenv()
        source_dir = _module_sandbox_dir()
        if not source_dir or not os.path.isdir(source_dir):
            raise UserError(
                "Bundled sandbox_docker directory not found in kensei2 module."
            )

        workdir = os.path.join(
            tempfile.gettempdir(),
            "kensei2-sandbox",
            "kensei2-%d-%s" % (self.kensei2_id.id, self.model_type),
        )
        if os.path.exists(workdir):
            shutil.rmtree(workdir)
        os.makedirs(workdir)

        for filename in ("Dockerfile", "litellm-patch-entrypoint.sh"):
            src = os.path.join(source_dir, filename)
            dst = os.path.join(workdir, filename)
            if os.path.isfile(src):
                shutil.copy2(src, dst)

        if persona.docker_compose_yaml:
            with open(os.path.join(workdir, "docker-compose.yml"), "w") as f:
                f.write(persona.docker_compose_yaml)
        else:
            src = os.path.join(source_dir, "docker-compose.yml")
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(workdir, "docker-compose.yml"))

        persona_dir = os.path.join(workdir, "personas", persona.name)
        os.makedirs(persona_dir)
        for fname, content in [
            ("SOUL.md", persona.soul_md),
            ("MEMORY.md", persona.memory_md),
            ("AGENTS.md", persona.agents_md),
        ]:
            if content:
                with open(os.path.join(persona_dir, fname), "w") as f:
                    f.write(content)

        data_dir = os.path.join(workdir, "data", persona.name)
        os.makedirs(data_dir, exist_ok=True)
        ws_dir = os.path.join(data_dir, "workspace")
        os.makedirs(os.path.join(ws_dir, "memory"), exist_ok=True)
        os.makedirs(os.path.join(ws_dir, "skills"), exist_ok=True)

        self._write_skill_files(ws_dir)
        mock_services = self._write_mock_service_dirs(workdir)

        for fname, content in [
            ("SOUL.md", persona.soul_md),
            ("MEMORY.md", persona.memory_md),
            ("AGENTS.md", persona.agents_md),
        ]:
            if content:
                with open(os.path.join(ws_dir, fname), "w") as f:
                    f.write(content)

        aws_bearer = (env.get("KENSEI2_AWS_BEARER_TOKEN") or env.get("AWS_BEARER_TOKEN_BEDROCK", "")).strip()
        aws_region = (env.get("KENSEI2_AWS_REGION") or env.get("AWS_REGION", "ap-south-1")).strip()
        bedrock_arn = (env.get("KENSEI2_BEDROCK_MODEL_ARN") or env.get("BEDROCK_MODEL_ARN", "")).strip()
        litellm_key = (env.get("KENSEI2_LITELLM_MASTER_KEY") or env.get("LITELLM_MASTER_KEY", "")).strip()
        if not litellm_key:
            litellm_key = "sk-kensei2-%s" % secrets.token_hex(8)

        origins = [
            "http://localhost:18789",
            "http://127.0.0.1:18789",
            "http://0.0.0.0:18789",
            "http://localhost:8069",
            "http://127.0.0.1:8069",
        ]
        if gateway_port != 18789:
            origins.append("http://localhost:%d" % gateway_port)
            origins.append("http://127.0.0.1:%d" % gateway_port)

        config = {
            "gateway": {
                "bind": "lan",
                "auth": {"mode": "token", "token": gateway_token},
                "trustedProxies": [
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "10.0.0.0/8",
                ],
                "controlUi": {
                    "allowedOrigins": origins,
                    "dangerouslyDisableDeviceAuth": True,
                },
                "http": {
                    "endpoints": {
                        "responses": {"enabled": True},
                    },
                },
            },
            "browser": {
                "enabled": False,
            },
            "tools": {
                "deny": ["browser"],
                "web": {
                    "search": {"enabled": False},
                    "fetch": {"enabled": False},
                },
            },
            "models": {"providers": {}},
        }

        providers = config["models"]["providers"]

        if aws_bearer and bedrock_arn:
            providers["kensei2-bedrock"] = {
                "baseUrl": "https://bedrock-runtime.%s.amazonaws.com" % aws_region,
                "apiKey": aws_bearer,
                "auth": "api-key",
                "api": "bedrock-converse-stream",
                "models": [
                    {
                        "id": bedrock_arn,
                        "name": "claude-inference",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                        "contextWindow": 200000,
                        "maxTokens": 128000,
                    }
                ],
            }
        providers["litellm"] = {
            "baseUrl": "http://litellm:4000/v1",
            "apiKey": litellm_key,
            "auth": "api-key",
            "api": "openai-completions",
            "models": [
                {
                    "id": "claude-opus-4.7",
                    "name": "claude-opus-4.7",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 200000,
                    "maxTokens": 128000,
                },
                {
                    "id": "kimi-k2.6",
                    "name": "kimi-k2.6",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 131072,
                    "maxTokens": 32768,
                },
                {
                    "id": "quiet_sand",
                    "name": "quiet_sand",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 131072,
                    "maxTokens": 32768,
                },
                {
                    "id": "gpt-5.5",
                    "name": "gpt-5.5",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 1050000,
                    "maxTokens": 128000,
                },
            ],
        }

        default_model = MODEL_DEFAULTS.get(self.model_type)
        if default_model:
            config["agents"] = {
                "defaults": {
                    "model": default_model,
                    "imageModel": {"primary": default_model},
                    "thinkingDefault": "xhigh",
                }
            }

        with open(os.path.join(data_dir, "openclaw.json"), "w") as f:
            json.dump(config, f)

        litellm_yaml = persona.litellm_config_yaml
        if not litellm_yaml:
            glm_arn = (env.get("KENSEI2_GLM_BEDROCK_MODEL_ARN") or env.get("GLM_BEDROCK_MODEL_ARN", "")).strip()
            glm_region = (env.get("KENSEI2_GLM_AWS_REGION") or env.get("GLM_AWS_REGION", "us-east-1")).strip()
            litellm_yaml = _DEFAULT_LITELLM_CONFIG.format(
                bedrock_arn=bedrock_arn or "PLACEHOLDER",
                aws_region=aws_region,
                glm_bedrock_arn=glm_arn or "PLACEHOLDER",
                glm_aws_region=glm_region,
            )
        with open(os.path.join(workdir, "litellm-config.yaml"), "w") as f:
            f.write(litellm_yaml)

        gog_config_dir = os.path.join(workdir, "gog-config")
        os.makedirs(os.path.join(gog_config_dir, "gogcli", "keyring"), exist_ok=True)
        gog_auth_raw = self.kensei2_id.gog_auth
        gog_auth_token_raw = self.kensei2_id.gog_auth_token
        _logger.info(
            "[GogAuth→Docker] task=%s gog_auth present=%s length=%s gog_auth_token present=%s length=%s",
            self.kensei2_id.id,
            bool(gog_auth_raw),
            len(gog_auth_raw) if gog_auth_raw else 0,
            bool(gog_auth_token_raw),
            len(gog_auth_token_raw) if gog_auth_token_raw else 0,
        )

        # --- Write client_secret.json from gog_auth (client credentials only) ---
        if gog_auth_raw:
            try:
                gog_data = json.loads(gog_auth_raw)
                if isinstance(gog_data, dict):
                    client_secret_obj = None
                    if "client_secret" in gog_data and isinstance(
                        gog_data["client_secret"], dict
                    ):
                        client_secret_obj = gog_data["client_secret"]
                    elif "installed" in gog_data or "web" in gog_data:
                        client_secret_obj = gog_data

                    if client_secret_obj:
                        cs_path = os.path.join(
                            gog_config_dir, "gogcli", "client_secret.json"
                        )
                        with open(cs_path, "w") as f:
                            json.dump(client_secret_obj, f)
                        _logger.info(
                            "[GogAuth→Docker] wrote client_secret.json to %s", cs_path
                        )
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    "[GogAuth→Docker] Could not parse gog_auth JSON for task %s",
                    self.kensei2_id.id,
                )

        # --- Write token/config files from gog_auth_token (auth tokens) ---
        if gog_auth_token_raw:
            try:
                token_data = json.loads(gog_auth_token_raw)
                if isinstance(token_data, dict):
                    gog_files = token_data.get("tokens", {})
                    written_files = []
                    for rel_path, content in gog_files.items():
                        if rel_path in ("client_secret", "tokens"):
                            continue
                        if not isinstance(content, str):
                            continue
                        abs_path = os.path.join(gog_config_dir, "gogcli", rel_path)
                        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                        with open(abs_path, "w") as f:
                            f.write(content)
                        written_files.append(rel_path)
                    _logger.info(
                        "[GogAuth→Docker] wrote %d token files to %s: %s",
                        len(written_files),
                        gog_config_dir,
                        written_files,
                    )
            except (json.JSONDecodeError, TypeError):
                _logger.warning(
                    "[GogAuth→Docker] Could not parse gog_auth_token JSON for task %s",
                    self.kensei2_id.id,
                )

        gog_cfg = os.path.join(gog_config_dir, "gogcli", "config.json")
        if not os.path.isfile(gog_cfg):
            with open(gog_cfg, "w") as f:
                json.dump({"keyring_backend": "file"}, f)

        nginx_conf = (
            "map $http_upgrade $connection_upgrade {\n"
            "    default upgrade;\n"
            "    ''      close;\n"
            "}\n"
            "server {\n"
            "    listen 80;\n"
            "    server_name _;\n"
            "    client_max_body_size 1650m;\n"
            "    proxy_buffering off;\n"
            "    location /browser-api/ {\n"
            "        proxy_pass http://openclaw:18791/;\n"
            "        proxy_http_version 1.1;\n"
            '        proxy_set_header Authorization "Bearer %s";\n'
            "        proxy_set_header Host localhost;\n"
            "        proxy_read_timeout 30s;\n"
            "        proxy_send_timeout 30s;\n"
            "    }\n"
            "    location /v1/ {\n"
            "        if ($request_method = 'OPTIONS') {\n"
            "            add_header 'Access-Control-Allow-Origin' '*';\n"
            "            add_header 'Access-Control-Allow-Methods' 'GET, POST, OPTIONS';\n"
            "            add_header 'Access-Control-Allow-Headers' 'Authorization, Content-Type, X-OpenClaw-Session-Key';\n"
            "            add_header 'Access-Control-Max-Age' 86400;\n"
            "            add_header 'Content-Length' 0;\n"
            "            add_header 'Content-Type' 'text/plain';\n"
            "            return 204;\n"
            "        }\n"
            "        add_header 'Access-Control-Allow-Origin' '*' always;\n"
            "        add_header 'Access-Control-Allow-Methods' 'GET, POST, OPTIONS' always;\n"
            "        add_header 'Access-Control-Allow-Headers' 'Authorization, Content-Type, X-OpenClaw-Session-Key' always;\n"
            "        proxy_pass http://openclaw:18789;\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Host localhost;\n"
            "        proxy_set_header Origin $http_origin;\n"
            "        proxy_set_header User-Agent $http_user_agent;\n"
            "        proxy_read_timeout 600s;\n"
            "        proxy_send_timeout 600s;\n"
            "    }\n"
            "    location / {\n"
            "        proxy_pass http://openclaw:18789;\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Upgrade $http_upgrade;\n"
            "        proxy_set_header Connection $connection_upgrade;\n"
            "        proxy_set_header Host localhost;\n"
            "        proxy_set_header Origin $http_origin;\n"
            "        proxy_set_header User-Agent $http_user_agent;\n"
            "        proxy_hide_header X-Frame-Options;\n"
            "        proxy_hide_header Content-Security-Policy;\n"
            "        proxy_read_timeout 600s;\n"
            "        proxy_send_timeout 600s;\n"
            "    }\n"
            "}\n"
        ) % gateway_token
        with open(os.path.join(workdir, "nginx.conf"), "w") as f:
            f.write(nginx_conf)

        override = (
            "services:\n"
            "  openclaw:\n"
            '    entrypoint: ["node", "openclaw.mjs", "gateway",'
            ' "--allow-unconfigured", "--token", "%s"]\n'
            "    command: []\n"
            "    ports: !override []\n"
            "    volumes:\n"
            "      - ./personas:/sandbox/personas:ro\n"
            "      - ./data/${PERSONA:-marcus}:/home/node/.openclaw\n"
            "      - ./gog-config:/home/node/.config:rw\n"
            "    environment:\n"
            "      - GOG_KEYRING_PASSWORD=${GOG_KEYRING_PASSWORD:-}\n"
            "      - GOG_ACCOUNT=${GOG_ACCOUNT:-}\n"
        ) % gateway_token

        for svc in mock_services:
            if svc["env_var_name"]:
                override += "      - %s=http://%s:%d\n" % (svc["env_var_name"], svc["name"], svc["port"])

        if mock_services:
            override += "    depends_on:\n"
            override += "      litellm:\n"
            override += "        condition: service_healthy\n"
            for svc in mock_services:
                override += "      %s:\n" % svc["name"]
                override += "        condition: service_healthy\n"

        override += (
            "  nginx:\n"
            "    image: nginx:alpine\n"
            "    depends_on:\n"
            "      - openclaw\n"
            "    volumes:\n"
            "      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro\n"
            "    ports:\n"
            '      - "%d:80"\n'
            "    networks:\n"
            "      - frontend\n"
            "  litellm:\n"
            "    ports:\n"
            '      - "%d:4000"\n'
            "  db:\n"
            "    ports:\n"
            '      - "%d:5432"\n'
        ) % (gateway_port, litellm_port, db_port)

        for svc in mock_services:
            override += "  %s:\n" % svc["name"]
            override += "    build:\n"
            override += "      context: ./%s\n" % svc["name"]
            override += "    expose:\n"
            override += '      - "%d"\n' % svc["port"]
            override += "    healthcheck:\n"
            override += (
                '      test: ["CMD", "python3", "-c", '
                '"import urllib.request; urllib.request.urlopen('
                "'http://localhost:%d%s')\"]"
                "\n"
            ) % (svc["port"], svc["healthcheck_path"])
            override += "      interval: 2s\n"
            override += "      timeout: 5s\n"
            override += "      retries: 15\n"
            override += "      start_period: 5s\n"
            override += "    networks:\n"
            override += "      - backend\n"
            if svc.get("memory_limit"):
                mem = svc["memory_limit"]
                # Convert K8s format (256Mi) to Docker format (256m)
                if mem.endswith("Mi"):
                    mem = mem[:-2] + "m"
                elif mem.endswith("Gi"):
                    mem = mem[:-2] + "g"
                override += "    deploy:\n"
                override += "      resources:\n"
                override += "        limits:\n"
                override += "          memory: %s\n" % mem

        with open(os.path.join(workdir, "docker-compose.override.yml"), "w") as f:
            f.write(override)

        return workdir

    def _write_skill_files(self, ws_dir):
        """Copy skill directories from module's environment/skills/ into workspace/skills/."""
        from odoo.modules.module import get_module_path

        mod_path = get_module_path("kensei2")
        if not mod_path:
            return
        env_skills_dir = os.path.join(mod_path, "environment", "skills")
        if not os.path.isdir(env_skills_dir):
            return
        dest_skills_dir = os.path.join(ws_dir, "skills")
        for entry in os.listdir(env_skills_dir):
            src = os.path.join(env_skills_dir, entry)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(dest_skills_dir, entry), dirs_exist_ok=True)

    def _write_mock_service_dirs(self, workdir):
        """Copy mock API service directories from module's environment/ into workdir."""
        from odoo.modules.module import get_module_path

        mod_path = get_module_path("kensei2")
        if not mod_path:
            return []
        env_dir = os.path.join(mod_path, "environment")
        if not os.path.isdir(env_dir):
            return []
        tracker_src = os.path.join(env_dir, "tracking_middleware.py")
        services = []
        for entry in sorted(os.listdir(env_dir)):
            svc_dir = os.path.join(env_dir, entry)
            toml_path = os.path.join(svc_dir, "service.toml")
            if not os.path.isfile(toml_path):
                continue
            svc_meta = self._parse_service_toml(toml_path)
            if not svc_meta:
                continue
            dest_dir = os.path.join(workdir, entry)
            shutil.copytree(svc_dir, dest_dir, dirs_exist_ok=True)
            if os.path.isfile(tracker_src):
                shutil.copy2(tracker_src, os.path.join(dest_dir, "tracking_middleware.py"))
            services.append(svc_meta)
        return services

    @staticmethod
    def _parse_service_toml(path):
        """Parse a service.toml file and return metadata dict."""
        try:
            if hasattr(__builtins__, "__import__"):
                import tomllib
            else:
                import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                return _parse_service_toml_fallback(path)
        with open(path, "rb") as f:
            data = tomllib.load(f)
        svc = data.get("service", {})
        k8s = data.get("k8s", {})
        return {
            "name": svc.get("name", ""),
            "port": svc.get("port", 0),
            "env_var_name": svc.get("env_var_name", ""),
            "healthcheck_path": svc.get("healthcheck_path", "/health"),
            "k8s_image": k8s.get("image", ""),
            "cpu_request": k8s.get("cpu_request", "25m"),
            "memory_request": k8s.get("memory_request", "128Mi"),
            "memory_limit": k8s.get("memory_limit", "256Mi"),
        }

    def _collect_mock_api_audit(self):
        """Collect request logs from mock API /audit/requests endpoints before shutdown."""
        self.ensure_one()
        if self.docker_status != "running":
            return

        mode = self._deployment_mode()
        if mode == "k8s":
            services = self.env['kensei2.sandbox.k8s']._load_environment_services()
            # For audit collection, try ALL services with a port — not just those
            # with k8s_image. Env vars are injected for all services, and sidecars
            # may be running even if image wasn't resolved at this point.
            reachable = [s for s in services if s.get("port")]
            _logger.info(
                "K8s audit: total_services=%d, reachable_with_port=%d (sandbox=%s)",
                len(services), len(reachable), self.id,
            )
            if reachable:
                self._collect_audit_k8s(reachable)
            else:
                _logger.warning(
                    "No mock services found for K8s audit (sandbox=%s). "
                    "Check environment/ directory and service.toml files.",
                    self.id,
                )
        else:
            from odoo.modules.module import get_module_path

            mod_path = get_module_path("kensei2")
            if not mod_path:
                return
            env_dir = os.path.join(mod_path, "environment")
            if not os.path.isdir(env_dir):
                return

            services = []
            for entry in sorted(os.listdir(env_dir)):
                toml_path = os.path.join(env_dir, entry, "service.toml")
                if not os.path.isfile(toml_path):
                    continue
                svc_meta = self._parse_service_toml(toml_path)
                if svc_meta:
                    services.append(svc_meta)

            if services:
                self._collect_audit_local(services)

    def _collect_audit_local(self, services):
        compose_bin = _compose_cmd()
        project_name = self.docker_compose_project
        workdir = self.docker_workdir
        if not compose_bin or not project_name or not workdir:
            return

        for svc in services:
            try:
                fetch_cmd = (
                    "import urllib.request, sys; "
                    "r = urllib.request.urlopen('http://localhost:%d/audit/requests'); "
                    "sys.stdout.write(r.read().decode())" % svc["port"]
                )
                cmd = compose_bin + ["-p", project_name]
                cmd += ["-f", "docker-compose.yml"]
                override = os.path.join(workdir, "docker-compose.override.yml")
                if os.path.isfile(override):
                    cmd += ["-f", "docker-compose.override.yml"]
                cmd += ["exec", "-T", svc["name"], "python3", "-c", fetch_cmd]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=workdir,
                )
                if result.returncode != 0:
                    _logger.debug(
                        "Audit collection failed for %s: %s",
                        svc["name"],
                        result.stderr[:200],
                    )
                    continue

                self._ingest_audit_json(svc["name"], result.stdout)
            except subprocess.TimeoutExpired:
                _logger.warning(
                    "Audit collection timed out for %s (sandbox=%s)",
                    svc["name"],
                    self.id,
                )
            except Exception as e:
                _logger.warning(
                    "Audit collection error for %s (sandbox=%s): %s",
                    svc["name"],
                    self.id,
                    e,
                )

    def _collect_audit_k8s(self, services):
        try:
            from kubernetes import client as k8s_client
            from kubernetes.stream import stream as k8s_stream
            from .kensei2_sandbox_k8s import _load_k8s_config
        except ImportError:
            _logger.debug("kubernetes package not available, skipping K8s audit collection")
            return

        try:
            _load_k8s_config()
        except Exception as exc:
            _logger.warning(
                "K8s audit: load_k8s_config failed (sandbox=%s): %s",
                self.id, exc,
            )
            return

        core_v1 = k8s_client.CoreV1Api()
        pod_label = "app.kubernetes.io/name=kensei2-sandbox,task-id=%s" % self.id
        namespace = "kensei2"

        try:
            pods = core_v1.list_namespaced_pod(
                namespace=namespace, label_selector=pod_label
            )
            if not pods.items:
                _logger.warning("No pod found for K8s audit (sandbox=%s, label=%s)", self.id, pod_label)
                return
            pod_name = pods.items[0].metadata.name
        except Exception as e:
            _logger.warning("Could not find K8s pod for sandbox %s: %s", self.id, e)
            return

        _logger.info(
            "K8s audit collection: pod=%s, services=%d (sandbox=%s)",
            pod_name, len(services), self.id,
        )

        for svc in services:
            try:
                fetch_cmd = [
                    "python3", "-c",
                    "import urllib.request, sys; "
                    "r = urllib.request.urlopen('http://localhost:%d/audit/requests'); "
                    "sys.stdout.write(r.read().decode())" % svc["port"],
                ]
                resp = k8s_stream(
                    core_v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    namespace,
                    container=svc["name"],
                    command=fetch_cmd,
                    stderr=False,
                    stdin=False,
                    stdout=True,
                    tty=False,
                    _preload_content=True,
                )
                if not resp or not resp.strip():
                    _logger.warning(
                        "K8s audit: empty response from %s:%d (sandbox=%s)",
                        svc["name"], svc["port"], self.id,
                    )
                    continue
                self._ingest_audit_json(svc["name"], resp)
            except Exception as e:
                _logger.warning(
                    "K8s audit collection error for %s (sandbox=%s): %s",
                    svc["name"],
                    self.id,
                    e,
                )

    def _ingest_audit_json(self, service_name, raw_json):
        import json as json_mod
        import ast
        from datetime import datetime

        stripped = (raw_json or "").strip()
        data = None
        try:
            data = json_mod.loads(stripped)
        except (json_mod.JSONDecodeError, ValueError, TypeError):
            # Fallback: mock APIs may return Python repr (single quotes) instead
            # of JSON when running older images or non-FastAPI responses.
            try:
                data = ast.literal_eval(stripped)
            except (ValueError, SyntaxError):
                pass

        if not isinstance(data, dict):
            _logger.warning(
                "K8s audit: unparseable response from %s (sandbox=%s) — raw[:200]: %s",
                service_name, self.id, stripped[:200],
            )
            return

        requests_list = data.get("requests", [])
        if not requests_list:
            _logger.info(
                "K8s audit: no requests logged by %s (sandbox=%s)",
                service_name, self.id,
            )
            return

        ApiRequest = self.env["kensei2.api.request"].sudo()

        for entry in requests_list:
            request_time = None
            ts_iso = entry.get("timestamp_iso")
            if ts_iso:
                try:
                    request_time = datetime.strptime(ts_iso, "%Y-%m-%dT%H:%M:%S")
                except (ValueError, TypeError):
                    pass

            vals = {
                "sandbox_id": self.id,
                "service_name": service_name,
                "method": entry.get("method", ""),
                "path": entry.get("path", ""),
                "query_params": json_mod.dumps(entry.get("query_params"))
                if entry.get("query_params")
                else False,
                "request_body": (
                    json_mod.dumps(entry["request_body"])
                    if isinstance(entry.get("request_body"), (dict, list))
                    else entry.get("request_body") or False
                ),
                "status_code": entry.get("status_code", 0),
                "response_body": (
                    json_mod.dumps(entry["response_body"])
                    if isinstance(entry.get("response_body"), (dict, list))
                    else entry.get("response_body") or False
                ),
                "request_time": request_time,
                "duration_ms": entry.get("duration_ms", 0),
            }
            ApiRequest.create(vals)

        _logger.info(
            "Collected %d audit entries from %s (sandbox=%s)",
            len(requests_list),
            service_name,
            self.id,
        )

    # ------------------------------------------------------------------
    # Test Generation & Execution
    # ------------------------------------------------------------------

    def _generate_intent_tests(self, prompt):
        """Generate tests from task intent (pre-execution), store as 'pending' for later run."""
        self.ensure_one()

        ICP = self.env["ir.config_parameter"].sudo()
        enabled = ICP.get_param("kensei2.test_gen_enabled", "True")
        if enabled.lower() in ("false", "0", "no"):
            _logger.info("Test generation disabled (sandbox=%s)", self.id)
            return

        if not prompt or not prompt.strip():
            _logger.info("No prompt provided, skipping test generation (sandbox=%s)", self.id)
            return

        TestResult = self.env["kensei2.test.result"].sudo()
        traj_field_map = {
            "claude": "claude_trajectory",
            "glm": "glm_trajectory",
            "gpt": "gpt_trajectory",
        }
        traj_field = traj_field_map.get(self.model_type, "")
        current_traj_index = 0
        if traj_field and self.kensei2_id:
            raw = getattr(self.kensei2_id, traj_field, "") or ""
            if raw.strip():
                try:
                    entries = json.loads(raw)
                    current_traj_index = len(entries) if isinstance(entries, list) else 0
                except (json.JSONDecodeError, TypeError):
                    pass
        result_record = TestResult.create({
            "sandbox_id": self.id,
            "model_used": "sonnet-4.6",
            "status": "generating",
            "trajectory_index": current_traj_index + 1,
        })

        try:
            system_prompt = self._load_intent_test_system_prompt()
            user_message = self._build_intent_test_user_message(prompt)
            result_record.write({"generation_prompt": user_message})

            inference_arn = ICP.get_param("kensei2.test_gen_inference_arn", "")
            if not inference_arn:
                inference_arn = ICP.get_param("kensei2.bedrock_inference_arn", "")
            if not inference_arn:
                result_record.write({
                    "status": "error",
                    "test_output": "No Bedrock inference ARN configured.",
                })
                return

            region = ICP.get_param("kensei2.bedrock_region", "ap-south-1")
            api_key = ICP.get_param("kensei2.aws_bearer_token", "")
            if not api_key:
                env_vars = _load_dotenv()
                api_key = env_vars.get(
                    "KENSEI2_AWS_BEARER_TOKEN",
                    env_vars.get("AWS_BEARER_TOKEN_BEDROCK", ""),
                )

            if not api_key:
                result_record.write({
                    "status": "error",
                    "test_output": "No AWS bearer token available.",
                })
                return

            gen_start = time.time()
            test_code, usage = self._call_test_gen_llm(
                api_key, inference_arn, region, system_prompt, user_message
            )
            gen_duration_ms = (time.time() - gen_start) * 1000

            if not test_code or not test_code.strip():
                result_record.write({
                    "status": "error",
                    "test_output": "LLM returned empty test code.",
                    "generation_tokens_in": usage.get("input_tokens", 0),
                    "generation_tokens_out": usage.get("output_tokens", 0),
                    "duration_generation_ms": gen_duration_ms,
                })
                return

            # Mark as 'pending' — code is ready, waiting for sandbox stop to execute
            result_record.write({
                "test_code": test_code,
                "generation_tokens_in": usage.get("input_tokens", 0),
                "generation_tokens_out": usage.get("output_tokens", 0),
                "duration_generation_ms": gen_duration_ms,
                "status": "pending",
            })

            _logger.info(
                "Intent-based test generation complete (sandbox=%s): "
                "code ready, status=pending, tokens_in=%d, tokens_out=%d",
                self.id,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
            )

        except Exception as e:
            _logger.exception("Intent test generation failed (sandbox=%s): %s", self.id, e)
            result_record.write({
                "status": "error",
                "test_output": "Exception during test generation: %s" % str(e)[:2000],
            })

    def _run_pending_tests(self):
        """Execute pending test.result records before container teardown."""
        self.ensure_one()

        if self.docker_status not in ("running", "stopping"):
            _logger.info(
                "Skipping pending test execution — sandbox not running (sandbox=%s, status=%s)",
                self.id, self.docker_status,
            )
            return

        pending_results = self.env["kensei2.test.result"].sudo().search([
            ("sandbox_id", "=", self.id),
            ("status", "=", "pending"),
        ])

        if not pending_results:
            _logger.info("No pending tests to run (sandbox=%s)", self.id)
            return

        for result_record in pending_results:
            test_code = result_record.test_code
            if not test_code or not test_code.strip():
                result_record.write({
                    "status": "error",
                    "test_output": "No test code available.",
                })
                continue

            result_record.write({"status": "running"})

            try:
                exec_start = time.time()
                test_output = self._execute_tests_in_sandbox(test_code)
                exec_duration_ms = (time.time() - exec_start) * 1000

                total, passed, failed, errored = self._parse_pytest_output(test_output)
                status = "passed" if failed == 0 and errored == 0 else "failed"

                result_record.write({
                    "test_output": test_output,
                    "tests_total": total,
                    "tests_passed": passed,
                    "tests_failed": failed,
                    "tests_errored": errored,
                    "duration_execution_ms": exec_duration_ms,
                    "status": status,
                })

                _logger.info(
                    "Pending test execution complete (sandbox=%s, result=%s): "
                    "%d total, %d passed, %d failed, %d errors",
                    self.id, result_record.id, total, passed, failed, errored,
                )

            except Exception as e:
                _logger.exception(
                    "Pending test execution failed (sandbox=%s, result=%s): %s",
                    self.id, result_record.id, e,
                )
                result_record.write({
                    "status": "error",
                    "test_output": "Exception during test execution: %s" % str(e)[:2000],
                })

    def _load_intent_test_system_prompt(self):
        """Load the intent-based test generation system prompt."""
        from odoo.modules.module import get_module_path

        module_path = get_module_path("kensei2")
        prompt_file = os.path.join(module_path, "intent_test_generation_prompt.md")
        if os.path.isfile(prompt_file):
            with open(prompt_file, "r") as f:
                return f.read()
        return (
            "You are a test engineer. Given a task instruction describing what an AI agent "
            "should do with mock APIs, generate pytest test cases that verify the expected "
            "state changes by querying the mock API GET endpoints.\n\n"
            "Rules:\n"
            "- Use only the `urllib.request` module (stdlib) for HTTP calls — do NOT use `requests`\n"
            "- Base URLs come from environment variables (e.g., os.environ['AMAZON_SELLER_API_URL'])\n"
            "- Test assertions verify the data state matches what the task instruction requires\n"
            "- Generate one test function per expected operation or logical group\n"
            "- Use descriptive test names: test_<service>_<operation>_<entity>\n"
            "- Include docstrings explaining what operation this verifies\n"
            "- Output ONLY valid Python code (no markdown fences, no explanations)\n"
            "- Import only: os, json, urllib.request, urllib.parse, pytest\n"
        )

    def _build_intent_test_user_message(self, prompt):
        from odoo.modules.module import get_module_path

        module_path = get_module_path("kensei2")
        api_docs_path = os.path.join(module_path, "environment", "API_DOCUMENTATION.md")
        api_docs = ""
        if os.path.isfile(api_docs_path):
            with open(api_docs_path, "r") as f:
                api_docs = f.read()
            if len(api_docs) > 30000:
                api_docs = api_docs[:30000] + "\n\n... [truncated]"

        env_dir = os.path.join(module_path, "environment")
        env_vars = {}
        if os.path.isdir(env_dir):
            for entry in sorted(os.listdir(env_dir)):
                svc_dir = os.path.join(env_dir, entry)
                toml_path = os.path.join(svc_dir, "service.toml")
                if os.path.isdir(svc_dir) and os.path.isfile(toml_path):
                    svc_meta = self._parse_service_toml(toml_path)
                    if svc_meta:
                        env_vars[svc_meta["name"]] = {
                            "env_var": svc_meta["env_var_name"],
                            "port": svc_meta["port"],
                        }

        task_toml = ""
        if self.kensei2_id:
            try:
                task_toml = self.kensei2_id._build_harbor_task_toml() or ""
            except Exception:
                pass

        message_parts = []

        message_parts.append("## Task Instruction (instruction.md)\n")
        message_parts.append("This is the prompt that will be sent to the AI agent. "
                             "Generate tests that verify the agent performed these actions correctly.\n\n")
        message_parts.append(prompt[:8000] if len(prompt) > 8000 else prompt)
        message_parts.append("\n")

        if task_toml:
            message_parts.append("\n## task.toml (distractor_skills and metadata)\n")
            message_parts.append("```toml\n%s\n```\n" % task_toml)

        message_parts.append("\n## Environment Variables for API Base URLs\n")
        message_parts.append("Use `os.environ.get('<ENV_VAR>', '<default>')` to get the full base URL.\n\n")
        for svc_name, info in env_vars.items():
            message_parts.append("- `%s` → service: %s (port %d, value will be like `http://localhost:%d`)\n" % (
                info["env_var"], svc_name, info["port"], info["port"]
            ))

        message_parts.append("\n## Mock API Documentation (endpoints for verification)\n")
        message_parts.append(api_docs)

        return "\n".join(message_parts)

    def _call_test_gen_llm(self, api_key, inference_arn, region, system_prompt, user_message):
        """Call Bedrock Converse API for test generation."""
        from urllib.parse import quote as url_quote

        url = "https://bedrock-runtime.%s.amazonaws.com/model/%s/converse" % (
            region, url_quote(inference_arn, safe=""),
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        }
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": user_message}],
                },
            ],
            "inferenceConfig": {
                "maxTokens": 12000,
                "temperature": 0.2,
            },
        }
        if system_prompt:
            payload["system"] = [{"text": system_prompt}]

        import httpx

        with httpx.Client(http2=True, timeout=120.0) as client:
            resp = client.post(url, json=payload, headers=headers)

        if resp.status_code != 200:
            raise RuntimeError(
                "Bedrock API error (HTTP %d): %s" % (resp.status_code, resp.text[:500])
            )

        result = resp.json()
        content_blocks = result.get("output", {}).get("message", {}).get("content", [])
        response_text = ""
        for block in content_blocks:
            if isinstance(block, dict) and "text" in block:
                response_text += block["text"]

        usage_raw = result.get("usage", {})
        usage = {
            "input_tokens": int(usage_raw.get("inputTokens", 0)),
            "output_tokens": int(usage_raw.get("outputTokens", 0)),
        }

        code = self._extract_python_code(response_text.strip())
        return code, usage

    @staticmethod
    def _extract_python_code(text):
        """Extract Python code from LLM response, stripping markdown fences."""
        pattern = r"```(?:python)?\s*\n(.*?)```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text

    def _execute_tests_in_sandbox(self, test_code):
        """Write test file and run pytest inside the sandbox container."""
        self.ensure_one()
        mode = self._deployment_mode()

        if mode == "k8s":
            return self._execute_tests_k8s(test_code)
        else:
            return self._execute_tests_local(test_code)

    def _execute_tests_local(self, test_code):
        """Execute tests inside the local Docker sandbox."""
        import base64

        workdir = self.docker_workdir
        if not workdir or not os.path.isdir(workdir):
            return "ERROR: Sandbox workdir not found: %s" % workdir

        compose_bin = _compose_cmd()
        project_name = "kensei2-%s-%s" % (self.kensei2_id.id, self.model_type)

        def _compose_exec(container, cmd_list):
            cmd = compose_bin + ["-p", project_name]
            cmd += ["-f", "docker-compose.yml"]
            override = os.path.join(workdir, "docker-compose.override.yml")
            if os.path.isfile(override):
                cmd += ["-f", "docker-compose.override.yml"]
            cmd += ["exec", "-T", container] + cmd_list
            return cmd

        encoded = base64.b64encode(test_code.encode()).decode()
        write_cmd = _compose_exec("openclaw", [
            "sh", "-c",
            "printf '%%s' '%s' | base64 -d > /tmp/test_state.py" % encoded,
        ])

        result = subprocess.run(
            write_cmd, capture_output=True, text=True, timeout=30, cwd=workdir
        )
        if result.returncode != 0:
            return "ERROR writing test file: %s" % result.stderr[:500]

        install_cmd = _compose_exec("openclaw", [
            "sh", "-c",
            "python3 -m pytest --version >/dev/null 2>&1 || "
            "(curl -sSL https://bootstrap.pypa.io/get-pip.py | python3 - --user --break-system-packages -q && "
            "python3 -m pip install --user --break-system-packages pytest -q) 2>&1 || true",
        ])
        subprocess.run(
            install_cmd, capture_output=True, text=True, timeout=60, cwd=workdir
        )

        run_cmd = _compose_exec("openclaw", [
            "python3", "-m", "pytest", "/tmp/test_state.py", "-v", "--tb=long",
        ])

        result = subprocess.run(
            run_cmd, capture_output=True, text=True, timeout=120, cwd=workdir
        )

        output = result.stdout
        if result.stderr:
            output += "\n--- STDERR ---\n" + result.stderr
        return output[:50000]

    def _execute_tests_k8s(self, test_code):
        """Execute tests inside the K8s sandbox pod."""
        try:
            from kubernetes import client as k8s_client
            from kubernetes.stream import stream as k8s_stream
            from .kensei2_sandbox_k8s import _load_k8s_config
        except ImportError:
            return "ERROR: kubernetes package not available"

        try:
            _load_k8s_config()
        except Exception as exc:
            _logger.warning("K8s test exec: load_k8s_config failed: %s", exc)
            return "ERROR: K8s config not available: %s" % str(exc)[:200]

        core_v1 = k8s_client.CoreV1Api()
        pod_label = "app.kubernetes.io/name=kensei2-sandbox,task-id=%s" % self.id
        namespace = "kensei2"

        try:
            pods = core_v1.list_namespaced_pod(
                namespace=namespace, label_selector=pod_label
            )
            if not pods.items:
                return "ERROR: No pod found for sandbox %s" % self.id
            pod_name = pods.items[0].metadata.name
        except Exception as e:
            return "ERROR finding pod: %s" % str(e)[:300]

        import base64
        encoded = base64.b64encode(test_code.encode()).decode()
        write_cmd = [
            "sh", "-c",
            "printf '%%s' '%s' | base64 -d > /tmp/test_state.py" % encoded,
        ]
        try:
            k8s_stream(
                core_v1.connect_get_namespaced_pod_exec,
                pod_name, namespace, container="openclaw",
                command=write_cmd,
                stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=True,
            )
        except Exception as e:
            return "ERROR writing test file to pod: %s" % str(e)[:300]

        try:
            k8s_stream(
                core_v1.connect_get_namespaced_pod_exec,
                pod_name, namespace, container="openclaw",
                command=[
                    "sh", "-c",
                    "python3 -m pytest --version >/dev/null 2>&1 || "
                    "(curl -sSL https://bootstrap.pypa.io/get-pip.py | python3 - --user --break-system-packages -q && "
                    "python3 -m pip install --user --break-system-packages pytest -q) 2>&1 || true",
                ],
                stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=True,
            )
        except Exception:
            pass

        try:
            stdout = k8s_stream(
                core_v1.connect_get_namespaced_pod_exec,
                pod_name, namespace, container="openclaw",
                command=["python3", "-m", "pytest", "/tmp/test_state.py", "-v", "--tb=long"],
                stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=True,
            )
            return (stdout or "")[:50000]
        except Exception as e:
            return "ERROR running pytest: %s" % str(e)[:500]

    @staticmethod
    def _parse_pytest_output(output):
        """Parse pytest output to extract test counts."""
        total = passed = failed = errored = 0

        # Match pytest summary line like: "5 passed, 2 failed, 1 error in 3.45s"
        # or "3 passed in 1.23s"
        summary_pattern = r"=+\s*(.*?)\s*=+"
        matches = re.findall(summary_pattern, output or "")
        if matches:
            summary = matches[-1]  # Take the last one (final summary)
            p = re.search(r"(\d+)\s+passed", summary)
            f = re.search(r"(\d+)\s+failed", summary)
            e = re.search(r"(\d+)\s+error", summary)

            if p:
                passed = int(p.group(1))
            if f:
                failed = int(f.group(1))
            if e:
                errored = int(e.group(1))
            total = passed + failed + errored
        else:
            # Fallback: count individual test lines
            passed = len(re.findall(r"PASSED", output or ""))
            failed = len(re.findall(r"FAILED", output or ""))
            errored = len(re.findall(r"ERROR", output or ""))
            total = passed + failed + errored

        return total, passed, failed, errored

    def _build_compose_env(self, gateway_token):
        """Build environment dict for docker compose subprocess."""
        self.ensure_one()
        persona = self.kensei2_id.persona_id

        env = _load_dotenv().copy()
        env["PERSONA"] = persona.name
        env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token

        if not (env.get("KENSEI2_LITELLM_MASTER_KEY") or env.get("LITELLM_MASTER_KEY")):
            # Derive from gateway_token so _query_litellm_spend can reconstruct
            # the same key without persistence. Random keys would drift between
            # boot and query, causing 401 against LiteLLM_VerificationTokenTable.
            env["LITELLM_MASTER_KEY"] = "sk-kensei2-%s" % gateway_token[:16]

        # Map KENSEI2_* env vars to the standard names docker-compose.yml expects.
        # This allows Kensei2 to use its own credentials while the compose file
        # continues to use generic ${VAR} interpolation.
        _kensei2_env_map = {
            "KENSEI2_AWS_BEARER_TOKEN": "AWS_BEARER_TOKEN_BEDROCK",
            "KENSEI2_AWS_REGION": "AWS_REGION",
            "KENSEI2_BEDROCK_MODEL_ARN": "BEDROCK_MODEL_ARN",
            "KENSEI2_LITELLM_MASTER_KEY": "LITELLM_MASTER_KEY",
            "KENSEI2_LITELLM_DB_PASSWORD": "LITELLM_DB_PASSWORD",
            "KENSEI2_MOONSHOT_API_KEY": "MOONSHOT_API_KEY",
            "KENSEI2_LLAMA_API_KEY": "LLAMA_API_KEY",
            "KENSEI2_OPENAI_API_KEY": "OPENAI_API_KEY",
            "KENSEI2_GLM_BEDROCK_MODEL_ARN": "GLM_BEDROCK_MODEL_ARN",
            "KENSEI2_GLM_AWS_REGION": "GLM_AWS_REGION",
        }
        for kensei2_key, standard_key in _kensei2_env_map.items():
            val = env.get(kensei2_key, "").strip()
            if val:
                env[standard_key] = val

        gog_kp = self.kensei2_id.password or ""
        if gog_kp:
            env["GOG_KEYRING_PASSWORD"] = gog_kp

        task_email = self.kensei2_id.email
        if task_email:
            env["GOG_ACCOUNT"] = task_email

        _logger.info(
            "[GogAuth→Docker] _build_compose_env task=%s GOG_ACCOUNT=%s GOG_KEYRING_PASSWORD=%s",
            self.kensei2_id.id,
            task_email or "(none)",
            "***set***" if gog_kp else "(empty)",
        )
        return env

    def _wait_for_health(self, compose_bin, project_name, workdir):
        import urllib.request

        deadline = time.monotonic() + _HEALTH_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    compose_bin
                    + ["-p", project_name, "ps", "--format", "json", "openclaw"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    cwd=workdir,
                )
                output = result.stdout.strip()
                if output:
                    try:
                        data = json.loads(output)
                    except json.JSONDecodeError:
                        data = json.loads(output.splitlines()[0])

                    if isinstance(data, list):
                        data = data[0] if data else {}

                    state = (data.get("State") or "").lower()
                    if state in ("exited", "dead"):
                        _logger.warning(
                            "openclaw container exited (project=%s, state=%s)",
                            project_name,
                            state,
                        )
                        return False
            except (subprocess.TimeoutExpired, Exception) as e:
                _logger.debug("Health poll compose-ps error: %s", e)

            try:
                urllib.request.urlopen(
                    "http://localhost:%d/healthz" % self.docker_port,
                    timeout=5,
                )
                return True
            except Exception:
                pass

            time.sleep(_HEALTH_POLL_INTERVAL)

        return False

    def _capture_container_logs(self, compose_bin, project_name, workdir):
        try:
            result = subprocess.run(
                compose_bin + ["-p", project_name, "logs", "--tail", "30", "openclaw"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                cwd=workdir,
            )
            return result.stdout.strip() or result.stderr.strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Status reconciliation (local Docker)
    # ------------------------------------------------------------------

    def _check_local_status(self):
        """Check actual Docker container state and reconcile with DB status."""
        self.ensure_one()

        with _SANDBOX_LOCK:
            if self.id in _SANDBOX_STARTING:
                return

        if not self.docker_compose_project:
            if self.docker_status not in ("stopped",):
                self.write({"docker_status": "stopped"})
            return

        compose_bin = _compose_cmd()
        if not compose_bin:
            return

        workdir = self.docker_workdir
        try:
            cmd = compose_bin + [
                "-p",
                self.docker_compose_project,
                "ps",
                "--format",
                "json",
                "openclaw",
            ]
            kw = {
                "capture_output": True,
                "text": True,
                "timeout": 10,
                "check": False,
            }
            if workdir and os.path.isdir(workdir):
                kw["cwd"] = workdir
            result = subprocess.run(cmd, **kw)

            output = result.stdout.strip()
            if not output:
                if self.docker_status == "starting":
                    # Containers may not exist yet (still building image).
                    # Leave as "starting" — the poll will check again later.
                    return
                if self.docker_status != "stopped":
                    _logger.info(
                        "[StatusCheck] No container found for project=%s sandbox=%s, marking stopped",
                        self.docker_compose_project,
                        self.id,
                    )
                    self.write(
                        {
                            "docker_status": "stopped",
                            "docker_compose_project": False,
                            "docker_port": 0,
                            "docker_litellm_port": 0,
                            "docker_gateway_token": False,
                            "docker_workdir": False,
                            "docker_error": False,
                        }
                    )
                return

            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                data = json.loads(output.splitlines()[0])

            if isinstance(data, list):
                data = data[0] if data else {}

            state = (data.get("State") or "").lower()
            health = (data.get("Health") or "").lower()

            if state in ("exited", "dead"):
                if self.docker_status != "error":
                    _logger.info(
                        "[StatusCheck] Container exited for project=%s sandbox=%s (state=%s), marking error",
                        self.docker_compose_project,
                        self.id,
                        state,
                    )
                    self.write(
                        {
                            "docker_status": "error",
                            "docker_error": "Container exited unexpectedly (state=%s)"
                            % state,
                        }
                    )
            elif state == "running" and health == "unhealthy":
                if self.docker_status == "starting":
                    _logger.debug(
                        "[StatusCheck] Container unhealthy during startup for project=%s sandbox=%s, "
                        "waiting for health check to pass",
                        self.docker_compose_project,
                        self.id,
                    )
                elif self.docker_status != "error":
                    _logger.info(
                        "[StatusCheck] Container unhealthy for project=%s sandbox=%s, marking error",
                        self.docker_compose_project,
                        self.id,
                    )
                    self.write(
                        {
                            "docker_status": "error",
                            "docker_error": "Container running but unhealthy",
                        }
                    )
            elif state == "running" and health in ("", "healthy"):
                if self.docker_status != "running":
                    _logger.info(
                        "[StatusCheck] Container running for project=%s sandbox=%s, updating to running",
                        self.docker_compose_project,
                        self.id,
                    )
                    self.write({"docker_status": "running"})
            elif state == "running" and health == "starting":
                # Docker health check still running — container is up but
                # not yet confirmed healthy.  Leave as "starting" so the
                # frontend poll checks again in a few seconds.
                _logger.debug(
                    "[StatusCheck] Container running, health starting for project=%s sandbox=%s",
                    self.docker_compose_project,
                    self.id,
                )
            # else: "created", "restarting" etc → leave as "starting"

        except subprocess.TimeoutExpired:
            _logger.debug(
                "[StatusCheck] Timed out checking status for sandbox %s", self.id
            )
        except Exception as e:
            _logger.debug(
                "[StatusCheck] Error checking status for sandbox %s: %s", self.id, e
            )

    def action_check_status(self):
        """Public action: reconcile DB docker_status with actual Docker state.

        Called by the frontend on page load and during polling to fix stale
        statuses.  Returns a dict mapping sandbox_id → current docker_status.
        """
        mode = self._deployment_mode()
        k8s = self.env["kensei2.sandbox.k8s"] if mode == "k8s" else None
        result = {}
        for sandbox in self:
            if (
                sandbox.docker_status in ("stopped",)
                and not sandbox.docker_compose_project
            ):
                result[sandbox.id] = sandbox.docker_status
                continue

            # Skip sandboxes that are actively being started in a background
            # thread — the thread will set the final status itself.
            with _SANDBOX_LOCK:
                if sandbox.id in _SANDBOX_STARTING:
                    result[sandbox.id] = sandbox.docker_status
                    continue

            if mode == "local":
                sandbox._check_local_status()
            elif mode == "k8s" and sandbox.docker_status in ("starting", "running"):
                try:
                    k8s_status = k8s.get_sandbox_status(sandbox)
                    if k8s_status != sandbox.docker_status:
                        vals = {"docker_status": k8s_status}
                        if k8s_status == "error":
                            vals["docker_error"] = (
                                "Sandbox deployment not found after timeout"
                            )
                        sandbox.write(vals)
                except Exception:
                    _logger.debug(
                        "[action_check_status] K8s status check failed for "
                        "sandbox %s, returning DB value",
                        sandbox.id,
                        exc_info=True,
                    )

            result[sandbox.id] = sandbox.docker_status
        return result

    # ------------------------------------------------------------------
    # Cron reconciliation (k8s)
    # ------------------------------------------------------------------

    @api.model
    def _cron_reconcile(self):
        mode = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("kensei2.deployment_mode", "local")
            .strip()
        )
        if mode != "k8s":
            return

        sandbox_ids = (
            self.sudo()
            .search([("docker_status", "in", ["starting", "running"])])
            .ids
        )
        if not sandbox_ids:
            return

        k8s = self.env["kensei2.sandbox.k8s"]
        db_name = self.env.cr.dbname
        for sid in sandbox_ids:
            try:
                status = k8s.get_sandbox_status(self.browse(sid))
            except Exception as e:
                _logger.error(
                    "[CRON] k8s status probe failed for sandbox %s: %s", sid, e
                )
                continue
            try:
                _retry_with_cursor(
                    db_name,
                    lambda env, _sid=sid, _st=status: _reconcile_one_sandbox(
                        env, _sid, _st
                    ),
                    label="cron_reconcile sandbox=%s" % sid,
                )
            except Exception as e:
                _logger.error(
                    "[CRON] Reconciliation error for sandbox %s: %s", sid, e
                )

    # ── Auto-process XML-RPC methods (called by consumer) ─────────────

    @api.model
    def auto_process_get_ws_info(self, sandbox_id):
        """Return WS connection details for a running sandbox."""
        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}
        if sandbox.docker_status != "running":
            return {
                "error": "Sandbox is not running (status=%s)" % sandbox.docker_status
            }

        mode = sandbox._deployment_mode()
        if mode == "k8s":
            ws_host = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("kensei2.ws_router_host", "")
                .strip()
            )
            if ws_host:
                ws_url = "wss://%s/sandbox/%s/" % (ws_host, sandbox.id)
            else:
                ws_url = ""
        else:
            ws_url = (
                "ws://localhost:%d" % sandbox.docker_port if sandbox.docker_port else ""
            )

        if not ws_url:
            return {"error": "Cannot determine WS URL"}

        return {
            "ws_url": ws_url,
            "gateway_token": sandbox.docker_gateway_token or "",
            "sandbox_id": sandbox.id,
        }

    @api.model
    def auto_process_create_turn(
        self,
        sandbox_id,
        message,
        is_hint=False,
        is_auto_hint=False,
        auto_hint_iteration=0,
        auto_hint_group_id="",
    ):
        """Create a turn record. Mirrors create_turn controller logic."""
        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        model_name = MODEL_DEFAULTS.get(sandbox.model_type, "unknown")
        next_num = len(sandbox.turn_ids) + 1
        is_hint_turn = bool(is_hint)

        vals = {
            "sandbox_id": sandbox.id,
            "turn_number": next_num,
            "model_name": model_name,
            "turn_status": "Pending",
            "is_hint_turn": is_hint_turn,
        }
        if is_hint_turn:
            vals["hints"] = message
        else:
            vals["prompt"] = message
        vals["prompt_timestamp"] = fields.Datetime.now()

        if is_auto_hint:
            vals["is_auto_hint"] = True
            vals["auto_hint_iteration"] = int(auto_hint_iteration or 0)
            if auto_hint_group_id:
                vals["auto_hint_group_id"] = auto_hint_group_id

        turn = self.env["kensei2.turn"].create(vals)

        if sandbox.session_status == "not_started":
            sandbox.sudo().write({"session_status": "in_progress"})

        return {"turn_id": turn.id}

    @api.model
    def auto_process_save_response(
        self,
        turn_id,
        response,
        tool_calls_json="",
        partial=False,
    ):
        """Save response to a turn. Mirrors save_response controller logic."""
        turn = self.env["kensei2.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        vals = {
            "response": response or "",
            "turn_status": "Streaming" if partial else "Completed",
        }
        if tool_calls_json:
            vals["tool_calls"] = tool_calls_json
        vals["response_timestamp"] = fields.Datetime.now()

        turn.write(vals)
        return {"success": True}

    @api.model
    def auto_process_save_trajectory(self, sandbox_id, turn_id, trajectory_json):
        """Save full trajectory JSON from chat.history to the turn."""
        turn = self.env["kensei2.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        if trajectory_json:
            if isinstance(trajectory_json, list):
                trajectory_json = json.dumps(trajectory_json, ensure_ascii=False)
            turn.write({"trajectory_messages": trajectory_json})

        return {"success": True}

    @api.model
    def auto_process_trigger_hint_eval(self, turn_id, sandbox_id):
        """Trigger auto-hint evaluation. Same logic as /kensei2/auto_hint_eval endpoint."""
        import uuid

        from ..controllers.auto_hint import _AUTO_HINT_POOL, _auto_hint_eval_bg

        ICP = self.env["ir.config_parameter"].sudo()
        if ICP.get_param("kensei2.disable_auto_hint", "False").lower() == "true":
            _logger.info(
                "auto_process_trigger_hint_eval: SKIPPED turn=%s sandbox=%s (disabled in Settings)",
                turn_id,
                sandbox_id,
            )
            return {"skipped": True, "reason": "Auto-Hint disabled in Settings"}

        turn = self.env["kensei2.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}
        if turn.turn_status != "Completed":
            return {"error": "Turn is not completed"}
        if not turn.response:
            return {"error": "Turn has no response"}

        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        current_iter = sandbox.auto_hint_iteration or 0
        if current_iter >= 5:
            return {"status": "max_retries"}

        group_id = sandbox.auto_hint_group_id or ""
        if current_iter == 0:
            group_id = uuid.uuid4().hex

        new_iter = current_iter + 1
        sandbox.write(
            {
                "auto_hint_status": "evaluating",
                "auto_hint_iteration": new_iter,
                "auto_hint_group_id": group_id,
            }
        )

        db_name = self.env.cr.dbname
        # Use admin partner for notifications (consumer is headless)
        notify_partner_id = self.env["res.users"].browse(SUPERUSER_ID).partner_id.id

        def _submit():
            _AUTO_HINT_POOL.submit(
                _auto_hint_eval_bg,
                db_name,
                sandbox_id,
                turn_id,
                group_id,
                new_iter,
                notify_partner_id,
            )

        self.env.cr.postcommit.add(_submit)

        return {"status": "pending", "iteration": new_iter, "group_id": group_id}

    @api.model
    def auto_process_poll_hint_status(self, sandbox_id):
        """Read current auto_hint_status and related data for polling."""
        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}

        result = {
            "auto_hint_status": sandbox.auto_hint_status or "idle",
            "auto_hint_iteration": sandbox.auto_hint_iteration or 0,
            "auto_hint_group_id": sandbox.auto_hint_group_id or "",
        }

        # Find the last turn and its feedback
        last_turn = sandbox.turn_ids.sorted("turn_number", reverse=True)[:1]
        if last_turn:
            result["last_turn_id"] = last_turn.id
            result["last_turn_feedback"] = last_turn.feedback or ""
            result["last_turn_hint_text"] = last_turn.hint_text or ""
        else:
            result["last_turn_id"] = 0
            result["last_turn_feedback"] = ""
            result["last_turn_hint_text"] = ""

        return result

    @api.model
    def auto_process_save_feedback(self, turn_id, feedback, hint_text=""):
        """Save feedback on a turn. Mirrors save_feedback controller logic."""
        turn = self.env["kensei2.turn"].browse(turn_id)
        if not turn.exists():
            return {"error": "Turn not found"}

        feedback = (feedback or "").strip().lower()
        if feedback not in ("satisfied", "unsatisfied"):
            return {"error": "Invalid feedback: %s" % feedback}

        vals = {"feedback": feedback}
        if hint_text:
            vals["hint_text"] = hint_text

        turn.write(vals)
        return {"success": True}

    @api.model
    def auto_process_reset_hint_status(self, sandbox_id):
        """Reset stuck auto_hint_status to idle (used on timeout)."""
        sandbox = self.browse(sandbox_id)
        if not sandbox.exists():
            return {"error": "Sandbox not found"}
        sandbox.write(
            {
                "auto_hint_status": "idle",
                "auto_hint_iteration": 0,
                "auto_hint_group_id": False,
            }
        )
        return {"success": True}
