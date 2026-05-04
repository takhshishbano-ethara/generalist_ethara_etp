"""GitHub sync helper for harness registry files (Option B).

When a user uploads a registry file via the Aurora UI, the wizard calls
``push_registry_to_github`` here to commit the file to the shared
multi-swe-bench repo. K8s worker pods then pick it up via the existing
``_sync_registry_from_github`` flow in ``tools/harness_bridge/phase2_docker_build.py``.
"""
from __future__ import annotations

import logging
from typing import Optional

from odoo.exceptions import UserError

from .credential_manager import get_encrypted_param

_logger = logging.getLogger(__name__)

_DEFAULT_REPO_SLUG = "EtharaAI/multi-swe-bench"
_DEFAULT_BRANCH = "main"
_DEFAULT_PATH_PREFIX = "multi_swe_bench/harness/repos"


def _build_path(lang: str, org: str, filename: str) -> str:
    return f"{_DEFAULT_PATH_PREFIX}/{lang}/{org}/{filename}"


def push_registry_to_github(
    env,
    lang: str,
    org: str,
    filename: str,
    content: str,
    commit_msg: str,
    author_name: str = "Aurora Pipeline",
    author_email: str = "aurora@ethara.ai",
    full_path: Optional[str] = None,
) -> Optional[dict]:
    """Commit a registry file to the configured harness GitHub repo.

    If ``full_path`` is provided, it is used as the path inside the repo
    (relative to repo root). Otherwise the path is built from lang/org/filename
    under ``multi_swe_bench/harness/repos/``.

    Returns a dict with {path, sha, html_url, commit_sha} on success, or
    None when no token is configured (graceful degradation for local dev).
    Raises UserError on push failure so the UI surfaces a clear message.
    """
    ICP = env["ir.config_parameter"].sudo()

    token = get_encrypted_param(env, "aurora.github_registry_write_token")
    if not token:
        _logger.warning(
            "aurora.github_registry_write_token not configured; skipping GitHub push "
            "for %s. K8s worker pods will NOT see this upload.",
            full_path or _build_path(lang, org, filename),
        )
        return None

    repo_slug = ICP.get_param("aurora.harness_git_repo", _DEFAULT_REPO_SLUG)
    branch = ICP.get_param("aurora.harness_git_branch", _DEFAULT_BRANCH)
    path = full_path or _build_path(lang, org, filename)

    try:
        from github import Auth, Github, GithubException, UnknownObjectException
    except ImportError as exc:
        raise UserError(
            "PyGithub is not installed. Add 'PyGithub' to the aurora module's "
            "external dependencies and restart Odoo."
        ) from exc

    try:
        gh = Github(auth=Auth.Token(token))
        repo = gh.get_repo(repo_slug)
    except GithubException as exc:
        raise UserError(
            f"Failed to authenticate with GitHub repo {repo_slug!r}: {exc}"
        ) from exc

    existing_sha: Optional[str] = None
    try:
        existing = repo.get_contents(path, ref=branch)
        if isinstance(existing, list):
            raise UserError(
                f"GitHub path {path!r} resolves to a directory, not a file. "
                "Registry layout on the remote is corrupted; investigate manually."
            )
        existing_sha = existing.sha
    except UnknownObjectException:
        existing_sha = None
    except GithubException as exc:
        raise UserError(
            f"Failed to read {path!r} from {repo_slug}@{branch}: {exc}"
        ) from exc

    author = {"name": author_name, "email": author_email}

    try:
        if existing_sha is None:
            result = repo.create_file(
                path=path,
                message=commit_msg,
                content=content,
                branch=branch,
                author=author,
                committer=author,
            )
        else:
            result = repo.update_file(
                path=path,
                message=commit_msg,
                content=content,
                sha=existing_sha,
                branch=branch,
                author=author,
                committer=author,
            )
    except GithubException as exc:
        raise UserError(
            f"GitHub commit failed for {path!r} on {repo_slug}@{branch}: {exc}. "
            "If this is a conflict, another user may have pushed a change; retry the upload."
        ) from exc

    content_obj = result.get("content") if isinstance(result, dict) else None
    commit_obj = result.get("commit") if isinstance(result, dict) else None

    info = {
        "path": path,
        "sha": getattr(content_obj, "sha", None) if content_obj else None,
        "html_url": getattr(content_obj, "html_url", None) if content_obj else None,
        "commit_sha": getattr(commit_obj, "sha", None) if commit_obj else None,
        "repo": repo_slug,
        "branch": branch,
    }

    _logger.info(
        "Pushed registry %s to %s@%s (commit=%s)",
        path, repo_slug, branch,
        info.get("commit_sha", "?")[:8] if info.get("commit_sha") else "?",
    )
    return info


def ensure_init_files_on_github(
    env,
    lang: str,
    org: str,
    repo: str,
    extra_stems: list[str] | None = None,
    author_name: str = "Aurora Pipeline",
    author_email: str = "aurora@ethara.ai",
) -> list[dict]:
    """Ensure the __init__.py files at the lang and org level import the registry.

    Called after ``push_registry_to_github`` so pods cloning the repo directly
    (or any external consumer of the GitHub tree) can import the harness via
    ``multi_swe_bench.harness.repos.{lang}.{org}.{repo}``.

    If ``extra_stems`` is provided (e.g. from a ZIP upload with range files),
    each stem gets its own ``from .{stem} import *`` line in the org __init__.py.
    Otherwise only the base repo_safe name is added.

    Behavior per init file:
      - If the file does not exist, create it with the correct import line.
      - If the file exists but is missing the import line, append it.
      - If the file exists and already contains the import line, skip (no API call).

    Returns a list of push_info dicts for the init files that were actually
    committed. Empty list means everything was already up to date.
    """
    ICP = env["ir.config_parameter"].sudo()
    token = get_encrypted_param(env, "aurora.github_registry_write_token")
    if not token:
        return []

    repo_slug = ICP.get_param("aurora.harness_git_repo", _DEFAULT_REPO_SLUG)
    branch = ICP.get_param("aurora.harness_git_branch", _DEFAULT_BRANCH)
    repo_safe = repo.replace("-", "_").lower()

    stems_to_import = extra_stems if extra_stems else [repo_safe]

    try:
        from github import Auth, Github, GithubException, UnknownObjectException
    except ImportError as exc:
        raise UserError(
            "PyGithub is not installed. Add 'PyGithub' to the aurora module's "
            "external dependencies and restart Odoo."
        ) from exc

    gh = Github(auth=Auth.Token(token))
    gh_repo = gh.get_repo(repo_slug)
    author = {"name": author_name, "email": author_email}

    def _read_remote(path: str) -> tuple[Optional[str], Optional[str]]:
        try:
            obj = gh_repo.get_contents(path, ref=branch)
        except UnknownObjectException:
            return None, None
        if isinstance(obj, list):
            raise UserError(
                f"GitHub path {path!r} resolves to a directory, not a file. "
                "Registry layout on the remote is corrupted; investigate manually."
            )
        return obj.decoded_content.decode("utf-8"), obj.sha

    def _commit(path: str, new_content: str, existing_sha: Optional[str], message: str) -> dict:
        try:
            if existing_sha is None:
                result = gh_repo.create_file(
                    path=path, message=message, content=new_content, branch=branch,
                    author=author, committer=author,
                )
            else:
                result = gh_repo.update_file(
                    path=path, message=message, content=new_content,
                    sha=existing_sha, branch=branch, author=author, committer=author,
                )
        except GithubException as exc:
            raise UserError(
                f"GitHub commit failed for {path!r} on {repo_slug}@{branch}: {exc}. "
                "If this is a conflict, another user may have pushed a change; retry the upload."
            ) from exc
        content_obj = result.get("content") if isinstance(result, dict) else None
        commit_obj = result.get("commit") if isinstance(result, dict) else None
        return {
            "path": path,
            "sha": getattr(content_obj, "sha", None) if content_obj else None,
            "html_url": getattr(content_obj, "html_url", None) if content_obj else None,
            "commit_sha": getattr(commit_obj, "sha", None) if commit_obj else None,
        }

    pushed: list[dict] = []

    org_init_path = f"{_DEFAULT_PATH_PREFIX}/{lang}/{org}/__init__.py"
    org_content, org_sha = _read_remote(org_init_path)

    missing_imports: list[str] = []
    for stem in stems_to_import:
        import_line = f"from .{stem} import *"
        if org_content is None or import_line not in org_content:
            missing_imports.append(import_line)

    if missing_imports:
        if org_content is None:
            new = "\n".join(missing_imports) + "\n"
            pushed.append(_commit(
                org_init_path, new, None,
                f"Create {lang}/{org}/__init__.py for {org}/{repo} via Aurora Harness Staging",
            ))
        else:
            new = org_content.rstrip("\n") + "\n" + "\n".join(missing_imports) + "\n"
            pushed.append(_commit(
                org_init_path, new, org_sha,
                f"Add imports to {lang}/{org}/__init__.py via Aurora Harness Staging",
            ))

    lang_init_path = f"{_DEFAULT_PATH_PREFIX}/{lang}/__init__.py"
    lang_import_line = f"from .{org} import *"
    lang_content, lang_sha = _read_remote(lang_init_path)
    if lang_content is None:
        new = f"{lang_import_line}\n"
        pushed.append(_commit(
            lang_init_path, new, None,
            f"Create {lang}/__init__.py for new org {org} via Aurora Harness Staging",
        ))
    elif lang_import_line not in lang_content:
        new = lang_content.rstrip("\n") + f"\n{lang_import_line}\n"
        pushed.append(_commit(
            lang_init_path, new, lang_sha,
            f"Add {org} import to {lang}/__init__.py via Aurora Harness Staging",
        ))

    if pushed:
        _logger.info(
            "Pushed %d __init__.py update(s) for %s/%s (lang=%s)",
            len(pushed), org, repo, lang,
        )
    return pushed
