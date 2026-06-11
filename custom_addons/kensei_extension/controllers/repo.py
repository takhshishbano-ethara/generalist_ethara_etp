import json
import logging
import re
from urllib.parse import urlparse, quote

import requests

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 30
MAX_TREE_ITEMS = 10000


def _json_response(message="", status=200, data=None, errors=None):
    payload = {
        "status": status,
        "message": message,
        "errors": errors,
        "data": data,
    }
    return Response(
        json.dumps(payload, default=str),
        status=status,
        content_type="application/json",
    )


def _read_params():
    params = dict(request.params or {})
    try:
        raw_body = request.httprequest.get_data(as_text=True) or ""
        if raw_body:
            body = json.loads(raw_body)
            if isinstance(body, dict):
                params.update(body)
    except (json.JSONDecodeError, ValueError):
        pass
    return params


def _parse_git_url(url):
    url = (url or "").strip()
    if not url:
        return None, None, None

    m = re.match(r"^git@([^:]+):([^/]+)/(.+?)(?:\.git)?/?$", url)
    if m:
        host, owner, repo = m.groups()
        return host, owner, repo

    parsed = urlparse(url)
    if not parsed.netloc or not parsed.path:
        return None, None, None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return parsed.netloc, None, None
    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return parsed.netloc, owner, repo


def _gh_headers(token):
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "kensei-extension",
    }


def _gh_get(path, token, params=None):
    url = f"{GITHUB_API_BASE}{path}"
    return requests.get(
        url,
        headers=_gh_headers(token),
        params=params,
        timeout=DEFAULT_TIMEOUT,
    )


def _gh_error_message(resp):
    if resp is None:
        return "No response from GitHub API."
    try:
        body = resp.json()
        msg = body.get("message")
        if msg:
            return f"GitHub API: {msg}"
    except (ValueError, AttributeError):
        pass
    return f"GitHub API returned HTTP {resp.status_code}."


def _gh_error_payload(resp, operation, owner, repo, path=None, ref=None):
    status = resp.status_code if resp is not None else 500
    info = {
        "operation": operation,
        "owner": owner,
        "repo": repo,
    }
    if path is not None:
        info["path"] = path
    if ref is not None:
        info["ref"] = ref
    info["http_status"] = status

    hints = []
    if status == 404:
        hints.append(
            "Check that 'owner/repo' is correct and the token has access. "
            "For PRIVATE repos a classic PAT needs the 'repo' scope; "
            "a fine-grained PAT needs 'Contents: Read' on this repository."
        )
        if path:
            hints.append(f"Verify path '{path}' exists at ref '{ref or 'default branch'}'.")
        if ref:
            hints.append(f"Verify ref '{ref}' (branch/tag/SHA) exists.")
    elif status == 401:
        hints.append("Token is invalid or revoked.")
    elif status == 403:
        hints.append(
            "Forbidden. Likely rate-limit exhausted or token lacks required scope. "
            "Inspect the response 'message' for specifics."
        )
    if hints:
        info["hints"] = hints
    return status, info


def _last_commit_for_path(owner, repo, path, ref, token):
    empty = {
        "last_commit_sha": None,
        "last_commit_date": None,
        "last_commit_message": None,
        "last_commit_author_name": None,
        "last_commit_author_email": None,
        "last_commit_html_url": None,
    }
    params = {"per_page": 1}
    if path:
        params["path"] = path
    if ref:
        params["sha"] = ref
    try:
        resp = _gh_get(f"/repos/{owner}/{repo}/commits", token, params=params)
    except requests.RequestException:
        return empty
    if resp.status_code != 200:
        return empty
    try:
        commits = resp.json() or []
    except ValueError:
        return empty
    if not commits:
        return empty
    head = commits[0] or {}
    commit = head.get("commit") or {}
    author = commit.get("author") or {}
    return {
        "last_commit_sha": head.get("sha"),
        "last_commit_date": author.get("date"),
        "last_commit_message": commit.get("message"),
        "last_commit_author_name": author.get("name"),
        "last_commit_author_email": author.get("email"),
        "last_commit_html_url": head.get("html_url"),
    }


def _encode_path(path):
    return quote(path, safe="/")


def _list_contents(owner, repo, path, ref, token, include_last_commit):
    url_path = f"/repos/{owner}/{repo}/contents/{_encode_path(path)}".rstrip("/")
    params = {"ref": ref} if ref else None
    try:
        resp = _gh_get(url_path, token, params=params)
    except requests.RequestException as exc:
        return None, None, str(exc)
    if resp.status_code != 200:
        return None, resp, None

    try:
        raw = resp.json()
    except ValueError:
        return None, resp, "Invalid JSON from GitHub."

    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None, resp, "Unexpected response shape from GitHub."

    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        item = {
            "name": entry.get("name"),
            "path": entry.get("path"),
            "type": entry_type,
            "size": entry.get("size") if entry_type == "file" else None,
            "sha": entry.get("sha"),
            "html_url": entry.get("html_url"),
            "download_url": entry.get("download_url"),
        }
        if include_last_commit:
            item.update(_last_commit_for_path(owner, repo, entry.get("path"), ref, token))
        items.append(item)
    return items, resp, None


def _resolve_ref_sha(owner, repo, ref, token):
    target = ref
    if not target:
        try:
            repo_resp = _gh_get(f"/repos/{owner}/{repo}", token)
        except requests.RequestException as exc:
            return None, None, str(exc)
        if repo_resp.status_code != 200:
            return None, repo_resp, None
        try:
            target = (repo_resp.json() or {}).get("default_branch") or "main"
        except ValueError:
            target = "main"

    try:
        commit_resp = _gh_get(f"/repos/{owner}/{repo}/commits/{target}", token)
    except requests.RequestException as exc:
        return None, None, str(exc)
    if commit_resp.status_code != 200:
        return None, commit_resp, None
    try:
        return (commit_resp.json() or {}).get("sha"), commit_resp, None
    except ValueError:
        return None, commit_resp, "Invalid JSON from GitHub."


def _tree_recursive(owner, repo, ref, token):
    sha, resp, err = _resolve_ref_sha(owner, repo, ref, token)
    if err:
        return None, None, err
    if not sha:
        return None, resp, None

    try:
        tree_resp = _gh_get(
            f"/repos/{owner}/{repo}/git/trees/{sha}",
            token,
            params={"recursive": "1"},
        )
    except requests.RequestException as exc:
        return None, None, str(exc)
    if tree_resp.status_code != 200:
        return None, tree_resp, None
    try:
        body = tree_resp.json() or {}
    except ValueError:
        return None, tree_resp, "Invalid JSON from GitHub."

    truncated = bool(body.get("truncated"))
    tree = body.get("tree") or []
    if len(tree) > MAX_TREE_ITEMS:
        tree = tree[:MAX_TREE_ITEMS]
        truncated = True

    items = []
    for entry in tree:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        full_path = entry.get("path") or ""
        items.append({
            "name": full_path.rsplit("/", 1)[-1],
            "path": full_path,
            "type": "dir" if entry_type == "tree" else "file",
            "size": entry.get("size") if entry_type == "blob" else None,
            "sha": entry.get("sha"),
            "mode": entry.get("mode"),
        })
    return {"items": items, "truncated": truncated, "ref_sha": sha}, tree_resp, None


def _parse_bool(raw, default=False):
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class KenseiRepoController(http.Controller):

    @http.route(
        "/api/v1/kensei_ext/repo/contents",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def kensei_ext_repo_contents(self, **kwargs):
        params = _read_params()

        git_url = (params.get("git_url") or "").strip()
        token = (params.get("token") or "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").strip()
        path = (params.get("path") or "").strip().lstrip("/")
        ref = (params.get("ref") or "").strip() or None
        recursive = _parse_bool(params.get("recursive"), default=False)
        include_last_commit = _parse_bool(
            params.get("include_last_commit"), default=True
        )

        if not git_url:
            return _json_response(
                message="Missing required parameter 'git_url'.", status=400,
            )
        if not token:
            return _json_response(
                message="Missing required parameter 'token'.", status=400,
            )

        host, owner, repo = _parse_git_url(git_url)
        if not owner or not repo:
            return _json_response(
                message=(
                    f"Unable to parse git_url '{git_url}'. "
                    "Expected formats: 'https://github.com/owner/repo' or "
                    "'git@github.com:owner/repo.git'."
                ),
                status=400,
            )
        if host and "github" not in host.lower():
            return _json_response(
                message=(
                    f"Unsupported git provider '{host}'. "
                    "Only github.com is currently supported."
                ),
                status=400,
            )

        try:
            if recursive:
                tree, resp, err = _tree_recursive(owner, repo, ref, token)
                if err:
                    return _json_response(
                        message="Network error contacting GitHub.",
                        status=502,
                        errors=[err],
                    )
                if tree is None:
                    status, info = _gh_error_payload(
                        resp, "list_tree_recursive", owner, repo, ref=ref,
                    )
                    return _json_response(
                        message=_gh_error_message(resp),
                        status=status,
                        errors=[info],
                    )
                data = {
                    "repo": {"owner": owner, "repo": repo},
                    "ref": ref,
                    "ref_sha": tree["ref_sha"],
                    "recursive": True,
                    "truncated": tree["truncated"],
                    "items_count": len(tree["items"]),
                    "items": tree["items"],
                }
            else:
                items, resp, err = _list_contents(
                    owner, repo, path, ref, token, include_last_commit,
                )
                if err:
                    return _json_response(
                        message="Network error contacting GitHub.",
                        status=502,
                        errors=[err],
                    )
                if items is None:
                    status, info = _gh_error_payload(
                        resp, "list_contents", owner, repo, path=path, ref=ref,
                    )
                    return _json_response(
                        message=_gh_error_message(resp),
                        status=status,
                        errors=[info],
                    )
                data = {
                    "repo": {"owner": owner, "repo": repo},
                    "ref": ref,
                    "path": path,
                    "recursive": False,
                    "include_last_commit": include_last_commit,
                    "items_count": len(items),
                    "items": items,
                }
        except Exception as exc:
            _logger.exception("kensei_ext_repo_contents failed")
            return _json_response(
                message="Failed to fetch repository contents.",
                status=500,
                errors=[str(exc)],
            )

        return _json_response(message="OK", status=200, data=data)
