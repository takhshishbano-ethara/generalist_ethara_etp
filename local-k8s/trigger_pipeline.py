#!/usr/bin/env python3
"""Trigger an Aurora pipeline run via Odoo JSON-RPC.

Usage:
    python3 local-k8s/trigger_pipeline.py --org colinhacks --repo zod
    python3 local-k8s/trigger_pipeline.py --org colinhacks --repo zod --url http://localhost:30069

Requires: Odoo running with Aurora module installed.
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error


def jsonrpc(url, method, params):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": int(time.time() * 1000),
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    if "error" in result:
        raise RuntimeError(f"JSON-RPC error: {result['error']}")
    return result.get("result")


def authenticate(base_url, db, login, password):
    url = f"{base_url}/jsonrpc"
    result = jsonrpc(url, "call", {
        "service": "common",
        "method": "authenticate",
        "args": [db, login, password, {}],
    })
    if not result:
        raise RuntimeError(f"Authentication failed for {login}@{db}")
    return result


def call_model(base_url, db, uid, password, model, method, args, kwargs=None):
    url = f"{base_url}/jsonrpc"
    return jsonrpc(url, "call", {
        "service": "object",
        "method": "execute_kw",
        "args": [db, uid, password, model, method, args, kwargs or {}],
    })


def wait_for_odoo(base_url, timeout=300):
    print(f"Waiting for Odoo at {base_url} ...", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{base_url}/web/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    print(" ready!")
                    return True
        except (urllib.error.URLError, OSError):
            pass
        print(".", end="", flush=True)
        time.sleep(5)
    print(" TIMEOUT")
    return False


def main():
    parser = argparse.ArgumentParser(description="Trigger Aurora pipeline locally")
    parser.add_argument("--org", required=True, help="GitHub org (e.g. colinhacks)")
    parser.add_argument("--repo", required=True, help="GitHub repo (e.g. zod)")
    parser.add_argument("--url", default="http://localhost:30069", help="Odoo base URL")
    parser.add_argument("--db", default="odoo", help="Database name")
    parser.add_argument("--login", default="admin", help="Odoo login")
    parser.add_argument("--password", default="admin", help="Odoo password")
    parser.add_argument("--lang", default="typescript", help="Pipeline language")
    parser.add_argument("--no-wait", action="store_true", help="Skip waiting for Odoo")
    args = parser.parse_args()

    if not args.no_wait:
        if not wait_for_odoo(args.url):
            print("ERROR: Odoo not reachable. Is the cluster running?")
            sys.exit(1)

    print(f"Authenticating as {args.login}@{args.db}...")
    uid = authenticate(args.url, args.db, args.login, args.password)
    print(f"Authenticated (uid={uid})")

    print(f"Creating pipeline: {args.org}/{args.repo} (lang={args.lang})...")
    pipeline_id = call_model(
        args.url, args.db, uid, args.password,
        "aurora.pipeline", "create",
        [{
            "name": f"[LOCAL] {args.org}/{args.repo}",
            "github_org": args.org,
            "github_repo": args.repo,
            "language": args.lang,
        }],
    )
    print(f"Pipeline created: id={pipeline_id}")

    print("Triggering pipeline execution...")
    try:
        call_model(
            args.url, args.db, uid, args.password,
            "aurora.pipeline", "action_run_pipeline",
            [[pipeline_id]],
        )
        print(f"Pipeline {pipeline_id} started successfully!")
        print(f"\nMonitor at: {args.url}/web#model=aurora.pipeline&view_type=form&id={pipeline_id}")
    except RuntimeError as e:
        print(f"Pipeline trigger failed: {e}")
        print("This might be expected if GitHub tokens haven't been imported yet.")
        print(f"Go to {args.url}/web and import tokens, then run the pipeline manually.")
        sys.exit(1)


if __name__ == "__main__":
    main()
