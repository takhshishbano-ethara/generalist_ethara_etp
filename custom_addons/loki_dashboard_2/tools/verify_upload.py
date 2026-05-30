#!/usr/bin/env python3
"""Verify that every local WSI/PDF file has a matching object in S3.

For each file under static/src/wsi/dzi/ and static/docs/, computes the expected
S3 key and HEADs it. Reports missing or size-mismatched objects.

Usage:
    python tools/verify_upload.py                                    # all patients
    python tools/verify_upload.py --pid 3                            # one patient
    python tools/verify_upload.py --bucket BUCKET --prefix PREFIX    # override
    python tools/verify_upload.py --kind wsi                         # WSI only
    python tools/verify_upload.py --kind doc                         # docs only
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_HERE)
_DZI_ROOT = os.path.join(_MODULE_DIR, "static", "src", "wsi", "dzi")
_DOCS_ROOT = os.path.join(_MODULE_DIR, "static", "docs")

_DEFAULT_BUCKET = "production-grtlabs-tag"
_DEFAULT_REGION = "us-east-1"
_DEFAULT_PREFIX = "loki_dashboard"


def _iter_wsi(pid_filter):
    if not os.path.isdir(_DZI_ROOT):
        return
    for pid in sorted(os.listdir(_DZI_ROOT)):
        if pid.startswith("."):
            continue
        if pid_filter and pid != pid_filter:
            continue
        pdir = os.path.join(_DZI_ROOT, pid)
        if not os.path.isdir(pdir):
            continue
        for root, _dirs, files in os.walk(pdir):
            for fn in files:
                if fn == ".DS_Store":
                    continue
                local = os.path.join(root, fn)
                rel = os.path.relpath(local, pdir)
                yield pid, "wsi", local, rel


def _iter_docs(pid_filter):
    if not os.path.isdir(_DOCS_ROOT):
        return
    for pid in sorted(os.listdir(_DOCS_ROOT)):
        if pid.startswith("."):
            continue
        if pid_filter and pid != pid_filter:
            continue
        pdir = os.path.join(_DOCS_ROOT, pid)
        if not os.path.isdir(pdir):
            continue
        for root, _dirs, files in os.walk(pdir):
            for fn in files:
                if fn == ".DS_Store":
                    continue
                local = os.path.join(root, fn)
                rel = os.path.relpath(local, pdir)
                yield pid, "doc", local, rel


def _key_for(prefix, kind, pid, rel):
    sub = "wsi" if kind == "wsi" else "docs"
    return f"{prefix}/{sub}/patients/{pid}/{rel.replace(os.sep, '/')}"


def _head(client, bucket, key):
    from botocore.exceptions import ClientError
    try:
        resp = client.head_object(Bucket=bucket, Key=key)
        return ("ok", int(resp["ContentLength"]))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return ("missing", None)
        return (f"error:{code}", None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bucket", default=_DEFAULT_BUCKET)
    ap.add_argument("--region", default=_DEFAULT_REGION)
    ap.add_argument("--prefix", default=_DEFAULT_PREFIX)
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--pid", default=None, help="Only check this patient id")
    ap.add_argument("--kind", choices=("wsi", "doc", "both"), default="both")
    ap.add_argument("--workers", type=int, default=64)
    args = ap.parse_args(argv)

    import boto3
    from botocore.config import Config
    cfg = Config(region_name=args.region, signature_version="s3v4",
                 retries={"max_attempts": 3, "mode": "standard"})
    client = boto3.client("s3", endpoint_url=args.endpoint, config=cfg)

    items = []
    if args.kind in ("wsi", "both"):
        items.extend(_iter_wsi(args.pid))
    if args.kind in ("doc", "both"):
        items.extend(_iter_docs(args.pid))

    total = len(items)
    if not total:
        print("No local files to verify.")
        return 0

    print(f"Verifying {total} objects against s3://{args.bucket}/{args.prefix}/ ...")
    ok = missing = mismatch = errored = 0
    sample_bad = []

    def task(item):
        pid, kind, local, rel = item
        key = _key_for(args.prefix, kind, pid, rel)
        local_size = os.path.getsize(local)
        status, remote_size = _head(client, args.bucket, key)
        return pid, kind, local, key, local_size, status, remote_size

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, fut in enumerate(ex.map(task, items), 1):
            pid, kind, local, key, local_size, status, remote_size = fut
            if status == "ok":
                if remote_size != local_size:
                    mismatch += 1
                    if len(sample_bad) < 10:
                        sample_bad.append(("size", key, local_size, remote_size))
                else:
                    ok += 1
            elif status == "missing":
                missing += 1
                if len(sample_bad) < 10:
                    sample_bad.append(("missing", key, local_size, None))
            else:
                errored += 1
                if len(sample_bad) < 10:
                    sample_bad.append((status, key, local_size, None))
            if i % 5000 == 0:
                print(f"  {i}/{total}  ok={ok} missing={missing} mismatch={mismatch} err={errored}")

    print(f"\nResult: ok={ok}  missing={missing}  size_mismatch={mismatch}  errors={errored}  total={total}")
    if sample_bad:
        print("\nFirst issues:")
        for status, key, ls, rs in sample_bad:
            print(f"  [{status}] {key}  local={ls} remote={rs}")
    return 0 if (missing == 0 and mismatch == 0 and errored == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
