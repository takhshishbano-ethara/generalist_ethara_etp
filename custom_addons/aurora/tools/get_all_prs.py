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
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

try:
    from .util import get_tokens, TokenRotator
except ImportError:
    from util import get_tokens, TokenRotator

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
    parser.add_argument("--org", type=str, required=True, help="Organization name.")
    parser.add_argument("--repo", type=str, required=True, help="Repository name.")

    return parser


def main(tokens: list[str], out_dir: Path, org: str, repo: str):
    _logger.info("starting get all pull requests")
    _logger.info(f"Output directory: {out_dir}")
    _logger.info(f"Using {len(tokens)} token(s)")
    _logger.info(f"Org: {org}")
    _logger.info(f"Repo: {repo}")

    rotator = TokenRotator(tokens)
    g = rotator.get_client()
    r = g.get_repo(f"{org}/{repo}")

    def datetime_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    _RATE_CHECK_INTERVAL = 200

    with open(out_dir / f"{org}__{repo}_prs.jsonl", "w", encoding="utf-8") as file:
        count = 0
        for pull in tqdm(r.get_pulls(state="closed"), desc="Pull Requests"):
            if not pull.merged_at:
                continue
            count += 1
            if count % _RATE_CHECK_INTERVAL == 0:
                g = rotator.get_client()
                remaining, _ = g.rate_limiting
                if remaining < 100:
                    _logger.warning(
                        f"  Rate limit low ({remaining}) at PR #{count}. "
                        f"Rotating token."
                    )
            file.write(
                json.dumps(
                    {
                        "org": org,
                        "repo": repo,
                        "number": pull.number,
                        "state": pull.state,
                        "title": pull.title,
                        "body": pull.body,
                        "url": pull.url,
                        "id": pull.id,
                        "node_id": pull.node_id,
                        "html_url": pull.html_url,
                        "diff_url": pull.diff_url,
                        "patch_url": pull.patch_url,
                        "issue_url": pull.issue_url,
                        "created_at": datetime_serializer(pull.created_at),
                        "updated_at": datetime_serializer(pull.updated_at),
                        "closed_at": datetime_serializer(pull.closed_at),
                        "merged_at": datetime_serializer(pull.merged_at),
                        "merge_commit_sha": pull.merge_commit_sha,
                        "labels": [label.name for label in pull.labels],
                        "draft": pull.draft,
                        "commits_url": pull.commits_url,
                        "review_comments_url": pull.review_comments_url,
                        "review_comment_url": pull.review_comment_url,
                        "comments_url": pull.comments_url,
                        "base": pull.base.raw_data,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    tokens = get_tokens(args.tokens)

    main(tokens, Path.cwd() / args.out_dir, args.org, args.repo)
