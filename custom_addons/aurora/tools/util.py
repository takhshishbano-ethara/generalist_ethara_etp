# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates

#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at

#      http://www.apache.org/licenses/LICENSE-2.0

#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

# Adapted for the Aurora Pipeline Odoo addon (LGPL-3).
# Apache 2.0 is forward-compatible with LGPL-3 per ASF/FSF guidance.

import argparse
import hashlib
import logging
import os
import re
import subprocess
import time
import threading
from pathlib import Path

from github import Auth, Github

_logger = logging.getLogger(__name__)


class AuroraPipelineError(Exception):
    pass


_SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]+$')


def validate_name(value: str, label: str = "name") -> str:
    if not _SAFE_NAME_RE.match(value):
        raise AuroraPipelineError(
            f"Invalid {label}: {value!r}. "
            "Only alphanumeric characters, dots, hyphens, and underscores are allowed."
        )
    return value


def clone_repo_bare(
    org: str,
    repo: str,
    cache_dir: str,
    auth_token: str = "",
    timeout_clone: int = 600,
    timeout_fetch: int = 300,
) -> Path | None:
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    repo_path = cache_path / f"{org}__{repo}.git"

    if repo_path.exists():
        _logger.info("Fetching latest for cached %s/%s", org, repo)
        try:
            env = _git_env_with_token(auth_token) if auth_token else None
            subprocess.run(
                ["git", "-C", str(repo_path), "fetch", "--tags", "--force", "--quiet"],
                capture_output=True,
                text=True,
                timeout=timeout_fetch,
                env=env,
            )
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError) as e:
            _logger.warning("Warning: fetch failed for %s/%s: %s", org, repo, e)
        return repo_path

    _logger.info("Cloning %s/%s (bare, blobless)...", org, repo)
    url = f"https://github.com/{org}/{repo}.git"
    env = _git_env_with_token(auth_token) if auth_token else None
    try:
        result = subprocess.run(
            ["git", "clone", "--bare", "--filter=blob:none", url, str(repo_path)],
            capture_output=True,
            text=True,
            timeout=timeout_clone,
            env=env,
        )
    except FileNotFoundError:
        _logger.error("git not installed. Clone-based operations disabled.")
        return None
    except subprocess.TimeoutExpired:
        _logger.error("Clone timed out for %s/%s.", org, repo)
        return None

    if result.returncode != 0:
        stderr_safe = result.stderr.strip()
        if auth_token:
            stderr_safe = stderr_safe.replace(auth_token, "***")
        _logger.error("Clone failed for %s/%s: %s", org, repo, stderr_safe)
        return None

    _logger.info("Clone complete for %s/%s", org, repo)
    return repo_path


def _git_env_with_token(token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_ASKPASS"] = "echo"
    env["GIT_TERMINAL_PROMPT"] = "0"
    header = f"Authorization: Basic {_b64_token(token)}"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
    env["GIT_CONFIG_VALUE_0"] = header
    return env


def _b64_token(token: str) -> str:
    import base64
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


_RATE_LIMIT_FLOOR = 50
_RATE_LIMIT_SLEEP_SECONDS = 30
_RATE_LIMIT_CHECK_INTERVAL = 100


class TokenRotator:
    def __init__(self, tokens: list[str]):
        if not tokens:
            raise AuroraPipelineError("TokenRotator requires at least one token")
        self._tokens = list(tokens)
        self._index = 0
        self._lock = threading.Lock()
        self._rate_limit_event = threading.Event()
        self._call_counts: dict[int, int] = {i: 0 for i in range(len(tokens))}
        self._clients: dict[int, Github] = {}

    def _make_client(self, idx: int) -> Github:
        if idx not in self._clients:
            self._clients[idx] = Github(auth=Auth.Token(self._tokens[idx]), per_page=100)
        return self._clients[idx]

    def _pick_next_index(self) -> int:
        n = len(self._tokens)
        start = self._index

        for _ in range(n):
            idx = self._index % n
            self._index += 1
            client = self._make_client(idx)

            remaining, _ = client.rate_limiting
            if remaining > _RATE_LIMIT_FLOOR:
                return idx

        earliest_reset = float("inf")
        for i in range(n):
            c = self._make_client(i)
            _, reset_ts = c.rate_limiting
            if reset_ts < earliest_reset:
                earliest_reset = reset_ts

        wait = max(earliest_reset - time.time(), 0) + 5
        if wait > 0:
            _logger.info(f"  [TokenRotator] All {n} token(s) near rate limit. "
                        f"Sleeping {wait:.0f}s until reset...")
            self._lock.release()
            try:
                self._rate_limit_event.wait(timeout=wait)
                self._rate_limit_event.clear()
            finally:
                self._lock.acquire()

        self._index = start
        return self._index % n

    def get_client(self) -> Github:
        with self._lock:
            idx = self._pick_next_index()
            self._call_counts[idx] += 1
            return self._make_client(idx)

    def get_token(self) -> str:
        with self._lock:
            idx = self._pick_next_index()
            self._call_counts[idx] += 1
            return self._tokens[idx]

    @property
    def token_count(self) -> int:
        return len(self._tokens)

    def summary(self) -> str:
        parts = []
        for i, tok in enumerate(self._tokens):
            client = self._make_client(i)
            remaining, _ = client.rate_limiting
            parts.append(f"  token {i+1}: {remaining} remaining, {self._call_counts[i]} calls")
        return "\n".join(parts)

    def get_rate_limits(self) -> dict[str, dict]:
        result = {}
        for i, tok in enumerate(self._tokens):
            tok_hash = hashlib.sha256(tok.encode()).hexdigest()
            client = self._make_client(i)
            remaining, reset_ts = client.rate_limiting
            result[tok_hash] = {"remaining": remaining, "reset": reset_ts}
        return result


def parse_tokens(tokens: str | list[str] | Path) -> list[str]:
    """Parse tokens from a string, list, or file path."""
    if isinstance(tokens, list):
        return tokens
    elif isinstance(tokens, str):
        return [tokens]
    elif isinstance(tokens, Path):
        if not tokens.exists() or not tokens.is_file():
            raise ValueError(f"Token file {tokens} does not exist or is not a file.")
        with tokens.open("r", encoding="utf-8") as file:
            return [line.strip() for line in file if line.strip()]
    return []


def _load_env_tokens() -> list[str]:
    """Load tokens from a .env file (GITHUB_TOKENS=... or GITHUB_TOKEN=...)."""
    _MAX_PARENT_WALK = 3
    search_dirs = [Path.cwd()] + list(Path.cwd().parents)[:_MAX_PARENT_WALK]
    for directory in search_dirs:
        env_file = directory / ".env"
        if env_file.is_file():
            break
    else:
        return []

    tokens: list[str] = []
    with env_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in ("GITHUB_TOKENS", "GITHUB_TOKEN", "GH_TOKEN"):
                value = value.strip().strip("'\"")
                for tok in value.split(","):
                    tok = tok.strip()
                    if tok:
                        tokens.append(tok)
    return tokens


def find_default_token_file() -> Path | None:
    """Try to find a default token file in the current directory."""
    possible_files = ["token", "tokens", "token.txt", "tokens.txt"]
    for file_name in possible_files:
        file_path = Path.cwd() / file_name
        if file_path.exists() and file_path.is_file():
            return file_path
    return None


def get_tokens(tokens) -> list[str]:
    """Resolve tokens from CLI args, .env file, env vars, or token files."""
    if tokens is None:
        env_tokens = _load_env_tokens()
        if env_tokens:
            _logger.info(f"Loaded {len(env_tokens)} token(s) from .env file")
            return env_tokens

        default_token_file = find_default_token_file()
        if default_token_file is None:
            for var in ("GITHUB_TOKENS", "GITHUB_TOKEN", "GH_TOKEN"):
                val = os.environ.get(var, "").strip()
                if val:
                    env_list = [t.strip() for t in val.split(",") if t.strip()]
                    if env_list:
                        _logger.info(f"Loaded {len(env_list)} token(s) from ${var}")
                        return env_list
            raise AuroraPipelineError(
                "No tokens provided. Pass --tokens, set GITHUB_TOKENS in .env, or create a tokens file."
            )
        tokens = default_token_file
    else:
        tokens = tokens[0] if len(tokens) == 1 else tokens

    try:
        token_list = parse_tokens(tokens)
        if not token_list:
            raise ValueError("Token list is empty after parsing.")
    except ValueError as e:
        _logger.error(f"Error: {e}")
        raise AuroraPipelineError(str(e)) from e

    if not token_list:
        raise AuroraPipelineError("No tokens provided.")
    return token_list


def optional_int(value):
    """argparse type for optional integer arguments."""
    if value.lower() == "none" or value.lower() == "null" or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid integer value: {value}")
