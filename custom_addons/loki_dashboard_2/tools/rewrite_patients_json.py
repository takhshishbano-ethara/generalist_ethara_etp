#!/usr/bin/env python3
"""Rewrite patients.json so dzi_path / documents[].url point at the S3 routes.

Before:
    "dzi_path": "/loki_dashboard_2/static/src/wsi/dzi/3/WSI_3.1.dzi"
    "url":      "/loki_dashboard_2/static/docs/3/overview/Clinical_Data_3.pdf"

After:
    "dzi_path": "/loki2/asset/wsi/3/WSI_3.1.dzi"
    "url":      "/loki2/asset/doc/3/overview/Clinical_Data_3.pdf"

Idempotent: paths already in the new form are left alone.

Usage:
    python tools/rewrite_patients_json.py --dry-run
    python tools/rewrite_patients_json.py            # writes atomically
    python tools/rewrite_patients_json.py --revert   # back to legacy /loki_dashboard_2/static/... form
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "data", "Clinical_Data", "patients.json")
)

_NEW_WSI_PREFIX = "/loki2/asset/wsi/"
_NEW_DOC_PREFIX = "/loki2/asset/doc/"
_OLD_WSI_PREFIX = "/loki_dashboard_2/static/src/wsi/dzi/"
_OLD_DOCS_PREFIX = "/loki_dashboard_2/static/docs/"


def _to_new_wsi(pid: str, old: str) -> str:
    # /loki_dashboard_2/static/src/wsi/dzi/<pid>/<basename>.dzi  →  /loki2/asset/wsi/<pid>/<basename>.dzi
    if old.startswith(_NEW_WSI_PREFIX):
        return old
    if not old.startswith(_OLD_WSI_PREFIX):
        return old
    tail = old[len(_OLD_WSI_PREFIX):]
    parts = tail.split("/", 1)
    if len(parts) != 2:
        return old
    _legacy_pid, rest = parts
    return f"{_NEW_WSI_PREFIX}{pid}/{rest}"


def _to_new_doc(pid: str, old: str) -> str:
    # /loki_dashboard_2/static/docs/<pid>/<category>/<filename>  →  /loki2/asset/doc/<pid>/<category>/<filename>
    if old.startswith(_NEW_DOC_PREFIX):
        return old
    if not old.startswith(_OLD_DOCS_PREFIX):
        return old
    tail = old[len(_OLD_DOCS_PREFIX):]
    parts = tail.split("/", 1)
    if len(parts) != 2:
        return old
    _legacy_pid, rest = parts
    return f"{_NEW_DOC_PREFIX}{pid}/{rest}"


def _to_legacy_wsi(pid: str, old: str) -> str:
    if not old.startswith(_NEW_WSI_PREFIX):
        return old
    tail = old[len(_NEW_WSI_PREFIX):]
    parts = tail.split("/", 1)
    if len(parts) != 2:
        return old
    _, rest = parts
    return f"{_OLD_WSI_PREFIX}{pid}/{rest}"


def _to_legacy_doc(pid: str, old: str) -> str:
    if not old.startswith(_NEW_DOC_PREFIX):
        return old
    tail = old[len(_NEW_DOC_PREFIX):]
    parts = tail.split("/", 1)
    if len(parts) != 2:
        return old
    _, rest = parts
    return f"{_OLD_DOCS_PREFIX}{pid}/{rest}"


def transform(payload, revert: bool, pid_filter=None):
    changes = []
    for p in payload.get("patients", []):
        pid = str(p.get("id") or p.get("code") or "").strip()
        if not pid:
            continue
        if pid_filter and pid != pid_filter:
            continue
        for slide in p.get("wsi_slides") or []:
            old = slide.get("dzi_path") or ""
            new = _to_legacy_wsi(pid, old) if revert else _to_new_wsi(pid, old)
            if new != old:
                changes.append((pid, "dzi_path", old, new))
                slide["dzi_path"] = new
        for doc in p.get("documents") or []:
            old = doc.get("url") or ""
            new = _to_legacy_doc(pid, old) if revert else _to_new_doc(pid, old)
            if new != old:
                changes.append((pid, "doc.url", old, new))
                doc["url"] = new
    return changes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default=_DEFAULT_PATH, help="Path to patients.json")
    ap.add_argument("--dry-run", action="store_true", help="Print planned changes, don't write.")
    ap.add_argument("--revert", action="store_true", help="Reverse: new form → legacy form.")
    ap.add_argument("--pid", default=None, help="Only rewrite this patient id (others untouched).")
    args = ap.parse_args(argv)

    with open(args.path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    changes = transform(payload, revert=args.revert, pid_filter=args.pid)

    if not changes:
        print("No changes — patients.json already in the target form.")
        return 0

    direction = "REVERT (new → legacy)" if args.revert else "REWRITE (legacy → new)"
    print(f"{direction}: {len(changes)} replacements")
    for pid, field, old, new in changes[:20]:
        print(f"  patient {pid:>6}  {field:<10}  {old}\n                                → {new}")
    if len(changes) > 20:
        print(f"  ... and {len(changes) - 20} more")

    if args.dry_run:
        print("\n--dry-run set; not writing.")
        return 0

    # atomic write
    target_dir = os.path.dirname(args.path)
    fd, tmp_path = tempfile.mkstemp(prefix=".patients.", suffix=".json", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, args.path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    print(f"\nWrote {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
