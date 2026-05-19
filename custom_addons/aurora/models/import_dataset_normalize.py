"""Normalises external dataset records to the format Aurora's harness expects.

Accepts records in three common shapes:
  - Aurora LHT bundle (already has instance_id + tag_start/tag_end)
  - SWE-bench single-PR (has number, no tags, no instance_id)
  - External pipelines with a date-window label embedded in base.label
    (e.g. "window:2026-02-26..window:2026-03-10")

Idempotent: existing values are preserved; missing values are filled.
"""


def normalize_record(rec: dict) -> None:
    org = rec.get("org", "") or ""
    repo = rec.get("repo", "") or ""
    number = rec.get("number", 0) or 0

    if not rec.get("tag_start") or not rec.get("tag_end"):
        label = (rec.get("base") or {}).get("label", "")
        if isinstance(label, str) and ".." in label:
            ts, te = label.split("..", 1)
            if not rec.get("tag_start"):
                rec["tag_start"] = ts.strip()
            if not rec.get("tag_end"):
                rec["tag_end"] = te.strip()

    if not rec.get("instance_id"):
        ts = rec.get("tag_start") or ""
        te = rec.get("tag_end") or ""
        if ts and te:
            rec["instance_id"] = f"{org}__{repo}-{ts}..{te}"
        elif number:
            rec["instance_id"] = f"{org}__{repo}-{number}"

    rec.setdefault("pr_numbers", [number] if number else [])
    rec.setdefault(
        "pr_url",
        f"https://github.com/{org}/{repo}/pull/{number}"
        if org and repo and number else "",
    )
    rec.setdefault("hints", "")
    rec.setdefault("release_line", "")
    rec.setdefault("version_scheme", "")
    rec.setdefault("pr_attribution_method", "")
    rec.setdefault("number_interval", "")
    rec.setdefault("head", {"sha": "", "ref": "", "label": ""})
