#!/usr/bin/env python3
"""Convert .svs whole-slide images to Deep Zoom tiles using libvips.

Usage:
    python tools/generate_dzi.py [--data-dir PATH] [--output-dir PATH]
                                 [--patients-json PATH] [--smallest-first]

Requires `vips` on PATH (Homebrew: `brew install vips`,
Debian/Ubuntu: `apt-get install libvips-tools`).

Source layouts supported:
    flat   : <data-dir>/Data/WSI_<stem>.<n>.svs   (or top-level WSI_*.svs)
    nested : <data-dir>/Patient_<pid>/wsi/*.svs

Output is driven by patients.json `wsi_slides[].dzi_path` so generated tiles
land exactly where the dashboard expects them. Skips slides whose `<basename>.dzi`
already exists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Iterable


def _stem_from_pid(pid: str) -> str:
    m = re.match(r"\d+", pid)
    return m.group(0) if m else pid


def _scan_svs(data_dir: str) -> dict[tuple[str, str], str]:
    """Return {(pid_stem, basename): src_path} for every .svs we can find."""
    found: dict[tuple[str, str], str] = {}
    candidates: list[str] = []
    data_sub = os.path.join(data_dir, "Data")
    if os.path.isdir(data_sub):
        candidates.append(data_sub)
    candidates.append(data_dir)
    for root in candidates:
        for fn in os.listdir(root):
            path = os.path.join(root, fn)
            if os.path.isfile(path) and fn.lower().endswith(".svs"):
                m = re.match(r"^WSI[_](\d+)\.\d+\.svs$", fn, re.IGNORECASE)
                if m:
                    found[(m.group(1), os.path.splitext(fn)[0])] = path
    rx = re.compile(r"^Patient_(.+)$")
    if os.path.isdir(data_dir):
        for entry in os.listdir(data_dir):
            sub = os.path.join(data_dir, entry)
            if not os.path.isdir(sub):
                continue
            m = rx.match(entry)
            if not m:
                continue
            wsi_dir = os.path.join(sub, "wsi")
            if not os.path.isdir(wsi_dir):
                continue
            for fn in os.listdir(wsi_dir):
                if fn.lower().endswith(".svs"):
                    pid_stem = _stem_from_pid(m.group(1))
                    found[(pid_stem, os.path.splitext(fn)[0])] = os.path.join(wsi_dir, fn)
    return found


def _load_targets(patients_json: str, dzi_root: str) -> list[tuple[str, str, str, str]]:
    """Return [(pid, pid_stem, basename, output_path_without_ext)] from patients.json."""
    if not os.path.isfile(patients_json):
        return []
    with open(patients_json) as fh:
        data = json.load(fh)
    out: list[tuple[str, str, str, str]] = []
    for p in data.get("patients", []):
        pid = p.get("id")
        if not pid:
            continue
        for slide in p.get("wsi_slides", []) or []:
            slide_name = slide.get("slide") or ""
            basename = os.path.splitext(slide_name)[0] if slide_name else None
            if not basename:
                dzi_path = slide.get("dzi_path") or ""
                basename = os.path.splitext(os.path.basename(dzi_path))[0]
            if not basename:
                continue
            out.append((pid, _stem_from_pid(pid), basename, os.path.join(dzi_root, pid, basename)))
    return out


def _sort_targets_by_size(targets, src_index, smallest_first):
    if not smallest_first:
        return targets
    def keyfn(t):
        src = src_index.get((t[1], t[2]))
        try:
            return os.path.getsize(src) if src else 0
        except OSError:
            return 0
    return sorted(targets, key=keyfn)


def _run_vips(src: str, out_no_ext: str) -> bool:
    print(f"vips dzsave {src} → {out_no_ext}.dzi", flush=True)
    try:
        subprocess.run(["vips", "dzsave", src, out_no_ext], check=True)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"Failed: {os.path.basename(src)} ({exc})", file=sys.stderr)
        return False


def main(argv: Iterable[str] | None = None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    default_data = os.path.normpath(os.path.join(here, "..", "data", "Clinical_Data"))
    default_out = os.path.normpath(os.path.join(here, "..", "static", "src", "wsi", "dzi"))
    default_pj = os.path.normpath(os.path.join(here, "..", "data", "Clinical_Data", "patients.json"))
    parser = argparse.ArgumentParser(description="Generate DZI tiles from .svs slides")
    parser.add_argument("--data-dir", default=default_data,
                        help="Where to find .svs files (flat or nested layout).")
    parser.add_argument("--output-dir", default=default_out,
                        help="Root dir for DZI output; final tiles land in <out>/<pid>/.")
    parser.add_argument("--patients-json", default=default_pj,
                        help="patients.json used to derive pid → slide mapping.")
    parser.add_argument("--smallest-first", action="store_true",
                        help="Process smallest .svs first for quicker feedback.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if shutil.which("vips") is None:
        print(
            "libvips not found. Install via `brew install vips` (macOS) or "
            "`apt-get install libvips-tools` (Debian/Ubuntu).",
            file=sys.stderr,
        )
        return 1

    if not os.path.isdir(args.data_dir):
        print(f"Data dir not found: {args.data_dir}", file=sys.stderr)
        return 1

    src_index = _scan_svs(args.data_dir)
    if not src_index:
        print(f"No .svs files found under {args.data_dir}", file=sys.stderr)
        return 1

    targets = _load_targets(args.patients_json, args.output_dir)
    if not targets:
        print(
            f"No wsi_slides entries in {args.patients_json}; "
            "run ingest_excel.py first or pass --patients-json.",
            file=sys.stderr,
        )
        return 1

    targets = _sort_targets_by_size(targets, src_index, args.smallest_first)

    converted = skipped = failed = missing = 0
    for pid, pid_stem, basename, out_no_ext in targets:
        if os.path.exists(f"{out_no_ext}.dzi"):
            skipped += 1
            continue
        src = src_index.get((pid_stem, basename))
        if not src:
            print(f"Missing .svs source for {pid}/{basename}", file=sys.stderr)
            missing += 1
            continue
        os.makedirs(os.path.dirname(out_no_ext), exist_ok=True)
        if _run_vips(src, out_no_ext):
            converted += 1
        else:
            failed += 1

    print(f"Done. converted={converted} skipped={skipped} missing={missing} failed={failed}")
    return 0 if failed == 0 and missing == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
