#!/usr/bin/env python3
"""Trigger an Aurora Phase 2 evaluation via Odoo JSON-RPC.

Usage:
    python3 local-k8s/trigger_evaluation.py --pipeline-id 1
    python3 local-k8s/trigger_evaluation.py --pipeline-id 1 --url http://localhost:30069

Requires: Odoo running with Aurora module installed, and a completed Phase 1 pipeline.
"""
import argparse
import json
import os
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


def wait_for_odoo(base_url, timeout=120):
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


def ensure_github_token(base_url, db, uid, password):
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("WARNING: GITHUB_TOKEN env var not set. Phase 2 needs tokens in the pool.")
        print("  Import tokens via Odoo UI: Settings > Aurora > Import Tokens")
        return False

    existing = call_model(
        base_url, db, uid, password,
        "aurora.github.token", "search_count",
        [[["token_hash", "!=", False]]],
    )
    if existing and existing > 0:
        print(f"Token pool already has {existing} token(s). Skipping import.")
        return True

    try:
        call_model(
            base_url, db, uid, password,
            "aurora.github.token", "create",
            [{"name": "local-dev-token", "token": token}],
        )
        print("GitHub token imported into pool.")
        return True
    except RuntimeError as e:
        print(f"WARNING: Could not import token: {e}")
        print("  Import manually via Odoo UI: Settings > Aurora > Import Tokens")
        return False


def main():
    parser = argparse.ArgumentParser(description="Trigger Aurora Phase 2 evaluation locally")
    parser.add_argument("--pipeline-id", type=int, required=True,
                        help="ID of completed Phase 1 pipeline")
    parser.add_argument("--url", default="http://localhost:30069", help="Odoo base URL")
    parser.add_argument("--db", default="odoo", help="Database name")
    parser.add_argument("--login", default="admin", help="Odoo login")
    parser.add_argument("--password", default="admin", help="Odoo password")
    parser.add_argument("--no-wait", action="store_true", help="Skip waiting for Odoo")
    parser.add_argument("--force-build", action="store_true",
                        help="Force rebuild Docker images even if cached")
    parser.add_argument("--max-workers", type=int, default=2,
                        help="Max parallel workers for build/run (default: 2)")
    args = parser.parse_args()

    if not args.no_wait:
        if not wait_for_odoo(args.url):
            print("ERROR: Odoo not reachable. Is the cluster running?")
            sys.exit(1)

    print(f"Authenticating as {args.login}@{args.db}...")
    uid = authenticate(args.url, args.db, args.login, args.password)
    print(f"Authenticated (uid={uid})")

    ensure_github_token(args.url, args.db, uid, args.password)

    pipeline = call_model(
        args.url, args.db, uid, args.password,
        "aurora.pipeline", "read",
        [[args.pipeline_id], ["name", "state", "step6_file"]],
    )
    if not pipeline:
        print(f"ERROR: Pipeline {args.pipeline_id} not found.")
        sys.exit(1)

    pipeline = pipeline[0]
    print(f"Pipeline: {pipeline.get('name')} (state={pipeline.get('state')})")

    if pipeline.get("state") not in ("done", "completed"):
        print(f"WARNING: Pipeline state is '{pipeline.get('state')}', not 'done'.")
        print("  Phase 2 needs a completed Phase 1 pipeline with dataset file.")
        resp = input("Continue anyway? [y/N] ")
        if resp.lower() != "y":
            sys.exit(0)

    print(f"Creating evaluation for pipeline {args.pipeline_id}...")
    eval_vals = {
        "pipeline_id": args.pipeline_id,
        "force_build": args.force_build,
        "max_workers_build": args.max_workers,
        "max_workers_run": args.max_workers,
    }
    eval_id = call_model(
        args.url, args.db, uid, args.password,
        "aurora.evaluation", "create",
        [eval_vals],
    )
    print(f"Evaluation created: id={eval_id}")

    print("Launching evaluation (DinD pod will be created in cluster)...")
    try:
        call_model(
            args.url, args.db, uid, args.password,
            "aurora.evaluation", "action_run_evaluation",
            [[eval_id]],
        )
        print(f"Evaluation {eval_id} launched successfully!")
        print(f"\nMonitor at: {args.url}/web#model=aurora.evaluation&view_type=form&id={eval_id}")
        print(f"\nWatch pod status:")
        print(f"  kubectl get pods -n aurora --context=aurora-local -w")
        print(f"  kubectl logs -n aurora -l evaluation-id={eval_id} -c evaluation --context=aurora-local -f")
    except RuntimeError as e:
        print(f"Evaluation trigger failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
