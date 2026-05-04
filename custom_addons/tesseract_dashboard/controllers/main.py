import json
import os

from markupsafe import Markup

from odoo import http
from odoo.http import request


_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "tesseract_ots.jsonl",
)


def _derive_repo(repo_url):
    """`https://github.com/org/name` -> `org/name`. Trailing slashes tolerated."""
    if not repo_url:
        return ""
    parts = repo_url.rstrip("/").rsplit("/", 2)
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def _flatten_row(instance, model_run):
    """Collapse one (instance, model) pair into the flat shape the
    client-side matrix/drawer already expects, plus the three new
    evaluation-receipt fields (f2p_count, p2p_count, docker_uri)."""
    return {
        "sr_no": instance.get("sr_no"),
        "instance_id": instance.get("instance_id"),
        "repo_url": instance.get("repo_url", ""),
        "repo": _derive_repo(instance.get("repo_url", "")),
        "pr_url": instance.get("pr_url", ""),
        "issue_url": instance.get("issue_url", ""),
        "language": instance.get("language", ""),
        "difficulty": instance.get("difficulty", ""),
        "docker_uri": instance.get("docker_uri", ""),
        "trajectory_url": instance.get("trajectory", ""),
        "f2p_count": instance.get("f2p_count", 0),
        "p2p_count": instance.get("p2p_count", 0),
        "model": model_run.get("model", ""),
        "files_modified": model_run.get("total_files_modified", 0),
        "tool_calls": model_run.get("total_tool_calls", 0),
        "time_secs": model_run.get("time_of_completion_secs", 0),
        "pass_at_1": model_run.get("pass_at_1", "Fail"),
    }


def _read_instances():
    """Read tesseract_ots.jsonl and return a flat JSON array — one row
    per (instance, model) pair — in the shape the client JS consumes.
    Returns a JSON string (not a list) because the caller wraps it in
    Markup for inline injection OR serves it via the raw API endpoint."""
    rows = []
    try:
        with open(_DATA_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                instance = json.loads(line)
                for model_run in instance.get("models", []):
                    rows.append(_flatten_row(instance, model_run))
    except FileNotFoundError:
        pass
    return json.dumps(rows, separators=(",", ":"))


class TesseractShowcaseController(http.Controller):
    """Public-facing routes for the Tesseract showcase.

    - GET /tesseract                  HTML portal page (inlines dataset)
    - GET /tesseract/api/instances    JSON dataset (public, CORS=*)
    """

    @http.route(
        "/tesseract",
        type="http",
        auth="public",
        website=True,
        sitemap=True,
    )
    def showcase_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        values = {
            "trajectories_url": ICP.get_param(
                "tesseract_dashboard.trajectories_url", ""
            ) or "https://github.com/Ethara-Ai/tesseract",
            "dataset_url": ICP.get_param(
                "tesseract_dashboard.dataset_url", ""
            ) or "https://huggingface.co/datasets/ethara/tesseract",
            # Inlined into the page inside a
            # <script type="application/json" id="instances-data">
            # block so the client JS can parse it synchronously. The
            # /tesseract/api/instances endpoint below serves the same
            # data for async / cross-origin consumers.
            # Wrap as Markup so QWeb's `t-out` injects the JSON verbatim
            # (no HTML-entity escaping). Defuse any "</script>" the data
            # might contain so it can't break out of the wrapping tag.
            "instances_json": Markup(
                _read_instances().replace("</", "<\\/")
            ),
        }
        rendered = request.env["ir.qweb"]._render(
            "tesseract_dashboard.portal_showcase", values
        )
        return request.make_response(
            "<!DOCTYPE html>\n" + str(rendered),
            headers=[("Content-Type", "text/html; charset=utf-8")],
        )

    @http.route(
        "/tesseract/api/instances",
        type="http",
        auth="public",
        cors="*",
    )
    def api_instances(self, **kw):
        return http.Response(
            _read_instances(),
            content_type="application/json",
            status=200,
        )
