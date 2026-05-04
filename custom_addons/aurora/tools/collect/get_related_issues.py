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
    from .util import get_tokens, AuroraPipelineError, TokenRotator, validate_name
except ImportError:
    from util import get_tokens, AuroraPipelineError, TokenRotator, validate_name

_logger = logging.getLogger(__name__)


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A command-line tool for processing repositories."
    )
    parser.add_argument(
        "--out_dir", type=Path, required=True, help="Output directory path."
    )
    parser.add_argument(
        "--tokens",
        type=str,
        nargs="*",
        default=None,
        help="API token(s) or path to token file.",
    )
    parser.add_argument(
        "--filtered_prs_file", type=Path, required=True, help="Path to pull file."
    )

    return parser


def main(tokens, out_dir: Path, filtered_prs_file: Path, progress_callback=None):
    _logger.info("starting get all related issues")
    _logger.info(f"Output directory: {out_dir}")
    _logger.info(f"Using {len(tokens)} token(s)")
    _logger.info(f"Pull file: {filtered_prs_file}")

    org_repo_re = re.compile(r"(.+)__(.+?)_(?:lht_)?filtered_prs.jsonl")
    m = org_repo_re.match(filtered_prs_file.name)
    if not m:
        raise AuroraPipelineError(f"Invalid pull file name: {filtered_prs_file.name}")

    org = m.group(1)
    repo = m.group(2)
    validate_name(org, "org")
    validate_name(repo, "repo")
    _logger.info(f"Org: {org}")
    _logger.info(f"Repo: {repo}")

    target_issues: set[int] = set()
    with open(filtered_prs_file, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                pr = json.loads(line)
            except json.JSONDecodeError:
                continue
            for issue in pr.get("resolved_issues", []):
                if isinstance(issue, int):
                    target_issues.add(issue)
                elif isinstance(issue, dict):
                    num = issue.get("number")
                    if num is not None:
                        target_issues.add(num)

    if not target_issues:
        _logger.info("No resolved issues to fetch. Writing empty file.")
        out_file_path = out_dir / f"{org}__{repo}_related_issues.jsonl"
        out_file_path.write_text("")
        return

    rotator = TokenRotator(tokens)
    g = rotator.get_client()
    r = g.get_repo(f"{org}/{repo}")

    total = len(target_issues)
    _logger.info(f"Fetching {total} specific issues by number...")
    _PROGRESS_INTERVAL = 25

    with open(
        out_dir / f"{org}__{repo}_related_issues.jsonl", "w", encoding="utf-8"
    ) as out_file:
        processed = 0
        for issue_num in tqdm(sorted(target_issues), desc="Fetching issues"):
            try:
                issue = r.get_issue(issue_num)
                out_file.write(
                    json.dumps(
                        {
                            "org": org,
                            "repo": repo,
                            "number": issue.number,
                            "state": issue.state,
                            "title": issue.title,
                            "body": issue.body,
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                )
            except Exception as e:
                _logger.warning(f"  Warning: could not fetch issue #{issue_num}: {e}")
            processed += 1
            if progress_callback and processed % _PROGRESS_INTERVAL == 0:
                try:
                    progress_callback(processed, total)
                except Exception:
                    pass

if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    tokens = get_tokens(args.tokens)

    main(tokens, Path.cwd() / args.out_dir, args.filtered_prs_file)
