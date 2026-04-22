"""Fetch version tags with smart parsing and classification.

Vendored from multi-swe-bench/collect/get_version_tags.py.
"""
import json
import logging
import re
from pathlib import Path

from github import Auth, Github, GithubException
from packaging.version import InvalidVersion
from packaging.version import Version as PkgVersion

_logger = logging.getLogger(__name__)

# Semver / Calver regex patterns
_SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z\-]+(?:\.[0-9A-Za-z\-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z\-]+(?:\.[0-9A-Za-z\-]+)*))?$",
)

_CALVER_RE = re.compile(
    r"^v?(?P<year>20\d{2}|[2-9]\d)\.(?P<month>0?[1-9]|1[0-2])"
    r"(?:\.(?P<day>0?[1-9]|[12]\d|3[01]))?"
    r"(?:\.(?P<micro>\d+))?"
    r"(?:-(?P<pre>[0-9A-Za-z\-]+(?:\.[0-9A-Za-z\-]+)*))?$",
)

_PRE_RELEASE_IDENTIFIERS = frozenset({
    "alpha", "beta", "rc", "preview", "dev", "nightly",
    "snapshot", "canary", "pre", "next", "insiders",
})

_PRE_RELEASE_RE = re.compile(
    r"[-.](?:" + "|".join(_PRE_RELEASE_IDENTIFIERS) + r")(?:\.\d+|\d+)?",
    re.IGNORECASE,
)


def parse_tag(name):
    """Parse a tag name into a structured record with scheme classification."""
    clean = name.strip()

    sv = _SEMVER_RE.match(clean)
    if sv:
        major, minor, patch = int(sv.group("major")), int(sv.group("minor")), int(sv.group("patch"))
        pre = sv.group("pre")
        pre_sort = (0, pre or "") if pre else (1, "")
        return {
            "scheme": "semver", "major": major, "minor": minor, "patch": patch,
            "pre_release": pre, "release_line": f"{major}.{minor}",
            "is_pre_release": pre is not None,
            "sort_key": (0, major, minor, patch, pre_sort),
        }

    cv = _CALVER_RE.match(clean)
    if cv:
        year_raw = int(cv.group("year"))
        year = year_raw if year_raw >= 100 else 2000 + year_raw
        month = int(cv.group("month"))
        day = int(cv.group("day") or 0)
        micro = int(cv.group("micro") or 0)
        pre = cv.group("pre")
        pre_sort = (0, pre or "") if pre else (1, "")
        return {
            "scheme": "calver", "major": year, "minor": month, "patch": day,
            "year": year, "month": month, "day": day, "micro": micro,
            "pre_release": pre, "release_line": f"{year}.{month}",
            "is_pre_release": pre is not None,
            "sort_key": (1, year, month, day, micro, pre_sort),
        }

    pre = None
    pre_match = _PRE_RELEASE_RE.search(clean)
    if pre_match:
        pre = pre_match.group(0).lstrip("-.")

    try:
        pv = PkgVersion(clean.lstrip("vV"))
        sk = (2, pv.major, pv.minor, pv.micro, (1, ""))
    except InvalidVersion:
        sk = (2, 0, 0, 0, (0, clean))

    return {
        "scheme": "unknown", "major": 0, "minor": 0, "patch": 0,
        "pre_release": pre, "release_line": "unknown",
        "is_pre_release": pre is not None, "sort_key": sk,
    }


def main(token, out_dir, org, repo, max_tags=200):
    """Fetch version tags and write to JSONL.

    Args:
        token: GitHub PAT string or list of PATs.
        out_dir: Path to output directory.
        org: GitHub organization name.
        repo: GitHub repository name.
        max_tags: Maximum number of tags to fetch.

    Returns:
        Path: Path to the tags JSONL file.
    """
    import random

    if isinstance(token, list):
        token = random.choice(token)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _logger.info("Fetching version tags for %s/%s (max %d)", org, repo, max_tags)

    auth = Auth.Token(token)
    g = Github(auth=auth, per_page=100)
    r = g.get_repo(f"{org}/{repo}")

    tag_records = []
    count = 0

    for tag in r.get_tags():
        if count >= max_tags:
            break
        name = tag.name
        sha = tag.commit.sha
        if not name or not sha:
            continue
        try:
            commit = r.get_commit(sha)
            date = commit.commit.committer.date
            date_str = date.isoformat() if date else ""
        except GithubException:
            date_str = ""
        if not date_str:
            continue
        parsed = parse_tag(name)
        record = {"name": name, "sha": sha, "date": date_str, **parsed}
        tag_records.append(record)
        count += 1

    tag_records.sort(key=lambda t: t["sort_key"])

    out_path = out_dir / f"{org}__{repo}_tags.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for record in tag_records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    _logger.info("Wrote %d tags to %s", len(tag_records), out_path)
    return out_path
