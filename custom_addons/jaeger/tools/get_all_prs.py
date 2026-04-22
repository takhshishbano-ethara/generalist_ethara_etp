import json
import logging
from pathlib import Path

from .util import datetime_serializer

_logger = logging.getLogger(__name__)


def main(pool, out_dir, org, repo):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{org}__{repo}_prs.jsonl"

    _logger.info("Fetching all PRs for %s/%s", org, repo)

    g, token = pool.get_github_client(per_page=100)
    r = g.get_repo(f"{org}/{repo}")

    count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for pull in r.get_pulls("all"):
            f.write(
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
                + "\n",
            )
            count += 1
            _logger.info(
                "PR #%d [%d] state=%s merged=%s draft=%s labels=[%s] title=%.80s",
                pull.number, count, pull.state,
                "yes" if pull.merged_at else "no",
                "yes" if pull.draft else "no",
                ",".join(label.name for label in pull.labels),
                pull.title,
            )

    pool.report_from_client(g, token)
    _logger.info("Wrote %d PRs to %s", count, out_path)
    return out_path
