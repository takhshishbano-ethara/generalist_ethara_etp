# -*- coding: utf-8 -*-
import logging
import os

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


def _validate_repo_path(entry):
    """Return the validated clone_path for a repo entry, or raise."""
    clone_path = entry.clone_path
    if not clone_path:
        raise AccessError("No clone path set for this repository entry.")
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


class Commit0Controller(http.Controller):
    @http.route("/commit0/start_pipeline", type="json", auth="user")
    def start_pipeline(self, run_id):
        """Start a pipeline run in the background."""
        run = request.env["commit0.pipeline.run"].browse(int(run_id))
        if not run.exists():
            return {"error": "Pipeline run not found"}
        result = run.action_start_pipeline()
        return {"success": True, "state": run.state, "notification": result}

    @http.route("/commit0/pipeline_status", type="json", auth="user")
    def pipeline_status(self, run_id):
        """Get current pipeline status for polling."""
        run = request.env["commit0.pipeline.run"].browse(int(run_id))
        if not run.exists():
            return {"error": "Pipeline run not found"}
        entries = []
        for entry in run.repo_entry_ids:
            entries.append(
                {
                    "id": entry.id,
                    "name": entry.name,
                    "repo_name": entry.repo_name or "",
                    "state": entry.state,
                    "test_count": entry.test_count,
                    "error_message": entry.error_message or "",
                }
            )
        return {
            "id": run.id,
            "name": run.name,
            "state": run.state,
            "progress_pct": run.progress_pct,
            "repo_count": run.repo_count,
            "error_message": run.error_message or "",
            "start_time": str(run.start_time) if run.start_time else "",
            "end_time": str(run.end_time) if run.end_time else "",
            "entries": entries,
        }

    @http.route("/commit0/pipeline_logs", type="json", auth="user")
    def pipeline_logs(self, run_id):
        """Get latest pipeline log output."""
        run = request.env["commit0.pipeline.run"].browse(int(run_id))
        if not run.exists():
            return {"error": "Pipeline run not found"}
        return {
            "log_output": run.log_output or "",
            "state": run.state,
        }

    @http.route("/commit0/cancel_pipeline", type="json", auth="user")
    def cancel_pipeline(self, run_id):
        """Cancel a running pipeline."""
        run = request.env["commit0.pipeline.run"].browse(int(run_id))
        if not run.exists():
            return {"error": "Pipeline run not found"}
        run.action_cancel_pipeline()
        return {"success": True, "state": run.state}

    # ------------------------------------------------------------------
    # File Browser endpoints
    # ------------------------------------------------------------------

    @http.route("/commit0/file_tree", type="json", auth="user")
    def file_tree(self, entry_id):
        entry = request.env["commit0.repo.entry"].browse(int(entry_id))
        if not entry.exists():
            return {"error": "Repository entry not found"}

        real_root = _validate_repo_path(entry)
        tree = _scan_directory(real_root)
        return {"tree": tree, "repo_name": entry.repo_name or ""}

    @http.route("/commit0/file_content", type="json", auth="user")
    def file_content(self, entry_id, file_path):
        entry = request.env["commit0.repo.entry"].browse(int(entry_id))
        if not entry.exists():
            return {"error": "Repository entry not found"}

        real_root = _validate_repo_path(entry)
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
