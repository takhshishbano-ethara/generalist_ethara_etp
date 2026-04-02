# -*- coding: utf-8 -*-
import base64
import logging
import os
import urllib.request
import json as json_lib

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request

_logger = logging.getLogger(__name__)

# Max file size to serve (5 MB) — prevents reading huge binaries
MAX_FILE_SIZE = 5 * 1024 * 1024

# Extensions considered viewable in the code editor
TEXT_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".css",
    ".scss",
    ".less",
    ".html",
    ".htm",
    ".xml",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".conf",
    ".txt",
    ".md",
    ".rst",
    ".csv",
    ".sh",
    ".bash",
    ".zsh",
    ".bat",
    ".ps1",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".sql",
    ".graphql",
    ".proto",
    ".dockerfile",
    ".gitignore",
    ".editorconfig",
    ".flake8",
    ".pylintrc",
    ".env.example",
    ".prettierrc",
    ".eslintrc",
}

# File/directory names to always skip
SKIP_NAMES = {
    "__pycache__",
    ".git",
    "node_modules",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".eggs",
    "*.egg-info",
    ".venv",
    "venv",
    "env",
    ".env",
}


def _validate_clone_path(clone_path):
    """Return the validated real path for a clone directory, or raise."""
    if not clone_path:
        raise AccessError("No clone path set for this evaluation.")
    real_root = os.path.realpath(clone_path)
    if not os.path.isdir(real_root):
        raise AccessError("Clone directory does not exist: %s" % clone_path)
    return real_root


def _check_path_traversal(real_root, file_path):
    """Validate file_path stays within real_root. Returns the real full path."""
    full = os.path.realpath(os.path.join(real_root, file_path))
    if not full.startswith(real_root + os.sep) and full != real_root:
        raise AccessError("Path traversal detected.")
    return full


def _is_text_file(name):
    lower = name.lower()
    # Dotfiles without extension (e.g. .gitignore)
    if lower.startswith(".") and "." not in lower[1:]:
        return lower in TEXT_EXTENSIONS
    _, ext = os.path.splitext(lower)
    return ext in TEXT_EXTENSIONS


def _should_skip(name):
    return name in SKIP_NAMES or name.endswith(".egg-info")


def _scan_directory(root_path, rel_prefix=""):
    """Recursively scan a directory, returning a nested file tree structure.

    Returns a list of dicts:
        [{"name": "src", "path": "src", "is_dir": True, "children": [...]},
         {"name": "setup.py", "path": "setup.py", "is_dir": False}]
    """
    entries = []
    try:
        items = sorted(os.listdir(root_path))
    except PermissionError:
        return entries

    dirs = []
    files = []
    for item in items:
        if _should_skip(item):
            continue
        full = os.path.join(root_path, item)
        rel = os.path.join(rel_prefix, item) if rel_prefix else item
        if os.path.isdir(full):
            dirs.append({"name": item, "path": rel, "is_dir": True})
        elif _is_text_file(item):
            files.append({"name": item, "path": rel, "is_dir": False})

    # Directories first, then files — each sorted alphabetically
    for d in dirs:
        d["children"] = _scan_directory(os.path.join(root_path, d["name"]), d["path"])
        entries.append(d)
    entries.extend(files)

    return entries


def _detect_ace_mode(file_path):
    ext_map = {
        # Python
        ".py": "python",
        ".pyi": "python",
        ".pyx": "python",
        ".cfg": "python",
        ".ini": "python",
        ".conf": "python",
        ".toml": "python",
        ".txt": "python",
        ".md": "python",
        ".rst": "python",
        ".csv": "python",
        ".gitignore": "python",
        ".editorconfig": "python",
        ".flake8": "python",
        ".pylintrc": "python",
        ".env.example": "python",
        ".sh": "python",
        ".bash": "python",
        ".zsh": "python",
        ".bat": "python",
        ".ps1": "python",
        ".dockerfile": "python",
        ".sql": "python",
        ".graphql": "python",
        ".proto": "python",
        ".c": "python",
        ".cpp": "python",
        ".h": "python",
        ".hpp": "python",
        ".java": "python",
        ".go": "python",
        ".rs": "python",
        ".rb": "python",
        # JavaScript (also covers JSON, YAML, TypeScript)
        ".js": "javascript",
        ".ts": "javascript",
        ".jsx": "javascript",
        ".tsx": "javascript",
        ".json": "javascript",
        ".yaml": "javascript",
        ".yml": "javascript",
        ".prettierrc": "javascript",
        ".eslintrc": "javascript",
        # XML / HTML
        ".xml": "xml",
        ".html": "xml",
        ".htm": "xml",
        # SCSS / CSS
        ".css": "scss",
        ".scss": "scss",
        ".less": "scss",
    }
    _, ext = os.path.splitext(file_path.lower())
    if not ext and file_path.startswith("."):
        ext = file_path.lower()
    return ext_map.get(ext, "python")


def _extract_github_full_name(repo_url):
    url = (repo_url or "").strip().rstrip("/").replace(".git", "")
    if "github.com/" in url:
        parts = url.split("github.com/")[-1].split("/")
        if len(parts) >= 2:
            return "%s/%s" % (parts[0], parts[1])
    return None


def _github_api_get(path, token=""):
    url = "https://api.github.com%s" % path
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("User-Agent", "Kaiju-Pipeline")
    if token:
        req.add_header("Authorization", "token %s" % token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json_lib.loads(resp.read().decode())


def _github_tree_to_local_format(items, prefix=""):
    dirs = []
    files = []
    for item in items:
        name = item.get("name", "")
        if _should_skip(name):
            continue
        rel = "%s/%s" % (prefix, name) if prefix else name
        if item.get("type") == "dir":
            dirs.append({"name": name, "path": rel, "is_dir": True, "children": []})
        elif _is_text_file(name):
            files.append({"name": name, "path": rel, "is_dir": False})
    dirs.sort(key=lambda d: d["name"])
    files.sort(key=lambda f: f["name"])
    return dirs + files


def _github_tree_recursive(full_name, path="", token="", depth=0):
    if depth > 4:
        return []
    api_path = "/repos/%s/contents/%s" % (full_name, path)
    try:
        items = _github_api_get(api_path, token)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    entries = _github_tree_to_local_format(items, path)
    for entry in entries:
        if entry["is_dir"]:
            entry["children"] = _github_tree_recursive(
                full_name, entry["path"], token, depth + 1
            )
    return entries


class Commit0Controller(http.Controller):
    # ------------------------------------------------------------------
    # File Browser endpoints
    # ------------------------------------------------------------------

    @http.route("/commit0/file_tree", type="jsonrpc", auth="user")
    def file_tree(self, eval_id, path_field="clone_path"):
        evaluation = request.env["commit0.repo.evaluation"].browse(int(eval_id))
        if not evaluation.exists():
            return {"error": "Evaluation not found"}

        clone_path = evaluation[path_field]
        if clone_path and os.path.isdir(clone_path):
            real_root = _validate_clone_path(clone_path)
            tree = _scan_directory(real_root)
            return {"tree": tree, "repo_name": evaluation.repo_name or ""}

        full_name = _extract_github_full_name(evaluation.repo_url)
        if not full_name:
            return {"error": "No clone path or valid GitHub URL"}
        token = (
            request.env["ir.config_parameter"]
            .sudo()
            .get_param("commit0_pipeline.github_token", "")
        )
        tree = _github_tree_recursive(full_name, token=token)
        return {"tree": tree, "repo_name": evaluation.repo_name or ""}

    @http.route("/commit0/file_content", type="jsonrpc", auth="user")
    def file_content(self, eval_id, file_path, path_field="clone_path"):
        evaluation = request.env["commit0.repo.evaluation"].browse(int(eval_id))
        if not evaluation.exists():
            return {"error": "Evaluation not found"}

        clone_path = evaluation[path_field]
        if clone_path and os.path.isdir(clone_path):
            real_root = _validate_clone_path(clone_path)
            full_path = _check_path_traversal(real_root, file_path)
            if not os.path.isfile(full_path):
                return {"error": "File not found: %s" % file_path}
            file_size = os.path.getsize(full_path)
            if file_size > MAX_FILE_SIZE:
                return {
                    "error": "File too large (%d bytes). Max: %d bytes."
                    % (file_size, MAX_FILE_SIZE)
                }
            try:
                with open(full_path, "r", errors="replace") as f:
                    content = f.read()
            except Exception as e:
                return {"error": "Cannot read file: %s" % str(e)[:200]}
            return {
                "content": content,
                "path": file_path,
                "mode": _detect_ace_mode(file_path),
                "size": file_size,
            }

        full_name = _extract_github_full_name(evaluation.repo_url)
        if not full_name:
            return {"error": "No clone path or valid GitHub URL"}
        token = (
            request.env["ir.config_parameter"]
            .sudo()
            .get_param("commit0_pipeline.github_token", "")
        )
        try:
            api_path = "/repos/%s/contents/%s" % (full_name, file_path)
            data = _github_api_get(api_path, token)
            if isinstance(data, list):
                return {"error": "Path is a directory, not a file"}
            content_b64 = data.get("content", "")
            content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            return {
                "content": content,
                "path": file_path,
                "mode": _detect_ace_mode(file_path),
                "size": data.get("size", 0),
            }
        except Exception as e:
            return {"error": "GitHub API error: %s" % str(e)[:200]}
