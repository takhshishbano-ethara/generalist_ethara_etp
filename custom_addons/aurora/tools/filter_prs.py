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
import json
import logging
import re
from pathlib import Path

from tqdm import tqdm

try:
    from .util import get_tokens, AuroraPipelineError, TokenRotator
except ImportError:
    from util import get_tokens, AuroraPipelineError, TokenRotator


def _check_cancelled():
    try:
        from odoo.addons.aurora.worker.run_pipeline import check_cancelled
        check_cancelled()
    except ImportError:
        pass


_logger = logging.getLogger(__name__)


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A command-line tool for processing repositories."
    )
    parser.add_argument(
        "--tokens",
        type=str,
        nargs="*",
        default=None,
        help="API token(s) or path to token file.",
    )
    parser.add_argument(
        "--out_dir", type=Path, required=True, help="Output directory path."
    )
    parser.add_argument(
        "--prs_file", type=Path, required=True, help="Path to pull file."
    )
    parser.add_argument(
        "--skip-commit-message",
        type=bool,
        default=True,
        help="Skip commit message.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["swe", "lht"],
        default="lht",
        help="Filter mode: swe (require resolved issues) or lht (all merged PRs).",
    )

    return parser


def extract_resolved_issues(pull: dict) -> list[str]:
    issues_pat = re.compile(r"(\w+)\s+\#(\d+)")
    comments_pat = re.compile(r"(?s)<!--.*?-->")
    keywords = {
        "close",
        "closes",
        "closed",
        "fix",
        "fixes",
        "fixed",
        "resolve",
        "resolves",
        "resolved",
    }

    text = pull["title"] if pull["title"] else ""
    text += "\n" + (pull["body"] if pull["body"] else "")
    text += "\n" + "\n".join([commit["message"] for commit in pull["commits"]])

    text = comments_pat.sub("", text)
    resolved_issues = set()
    for word, issue_num in issues_pat.findall(text):
        if word.lower() in keywords:
            resolved_issues.add(int(issue_num))

    resolved_issues.discard(0)

    return list(resolved_issues)


def main(tokens: list[str], out_dir: Path, prs_file: Path, skip_commit_message: bool = True, mode: str = "lht"):
    _logger.info("starting filter to obtain required pull requests")
    _logger.info(f"Output directory: {out_dir}")
    _logger.info(f"All Pull Requests: {prs_file}")
    _logger.info(f"Skip commit message: {skip_commit_message}")
    _logger.info(f"Mode: {mode}")

    if skip_commit_message:
        _logger.warning(
            "Commit message analysis is DISABLED (skip_commit_message=True). "
            "Issue references in commit messages will NOT be detected. "
            "Only PR title/body will be searched for resolved issues."
        )

    org_repo_re = re.compile(r"(.+)__(.+)_prs.jsonl")
    m = org_repo_re.match(prs_file.name)
    if not m:
        raise AuroraPipelineError(f"Invalid pull file name: {prs_file.name}")

    org = m.group(1)
    repo = m.group(2)
    _logger.info(f"Org: {org}")
    _logger.info(f"Repo: {repo}")

    if not skip_commit_message:
        rotator = TokenRotator(tokens)
        g = rotator.get_client()
        r = g.get_repo(f"{org}/{repo}")

    # Determine output filename based on mode
    if mode == "lht":
        out_filename = f"{org}__{repo}_lht_filtered_prs.jsonl"
    else:
        out_filename = f"{org}__{repo}_filtered_prs.jsonl"

    with (
        open(
            out_dir / out_filename,
            "w",
            encoding="utf-8",
        ) as out_file,
        open(prs_file, "r", encoding="utf-8") as in_file,
    ):
        for line in tqdm(in_file, desc="Pull Requests"):
            _check_cancelled()
            line = line.strip()
            if not line:
                continue
            pull = json.loads(line)

            if pull["state"] != "closed":
                continue

            if mode == "lht" and not pull.get("merged_at"):
                continue

            pull["commits"] = []
            if not skip_commit_message:
                pr = r.get_pull(pull["number"])
                pull["commits"] = [
                    {
                        "sha": commit.sha,
                        "parents": [parent.sha for parent in commit.parents],
                        "message": commit.commit.message,
                    }
                    for commit in pr.get_commits()
                ]

            resolved_issues = extract_resolved_issues(pull)

            if len(resolved_issues) == 0:
                continue

            pull["resolved_issues"] = resolved_issues
            out_file.write(json.dumps(pull, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    tokens = get_tokens(args.tokens)

    main(tokens, Path.cwd() / args.out_dir, args.prs_file, args.skip_commit_message, args.mode)
