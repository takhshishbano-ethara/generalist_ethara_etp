#!/usr/bin/env python3
"""Build tron_dashboard/data/tron_instances.json from a harbor task folder.

Walks a directory of UUID-named task folders and extracts task.toml + verifiers.jsonl
+ seed filenames into a single JSON array consumed by the /tron dataset viewer.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = Path("/Users/apple/Downloads/final_30_harbor")
DEFAULT_OUT = REPO_ROOT / "custom_addons" / "tron_dashboard" / "data" / "tron_instances.json"

DOMAIN_LABELS = {
    "hr": "HR",
    "itsm": "ITSM",
    "csm": "CSM",
    "drive": "Drive",
    "email": "Email",
    "calendar": "Calendar",
    "hybrid": "Hybrid",
    "teams": "Teams",
}


def domain_label(category: str) -> str:
    key = (category or "").strip().lower()
    return DOMAIN_LABELS.get(key, key.title() if key else "Unknown")


def load_verifiers(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            v = json.loads(line)
            cfg = v.get("config") or {}
            out.append({
                "idx": v.get("idx"),
                "type": v.get("type"),
                "original_name": v.get("original_name"),
                "reward_key": v.get("reward_key"),
                "description": v.get("description"),
                "gym": v.get("gym"),
                "query": cfg.get("query"),
                "expected_value": cfg.get("expected_value"),
                "comparison_type": cfg.get("comparison_type"),
            })
    return out


def build_record(folder: Path) -> dict:
    toml_path = folder / "task.toml"
    with toml_path.open("rb") as fh:
        doc = tomllib.load(fh)

    task = doc.get("task", {})
    meta = doc.get("metadata", {})
    actor = meta.get("actor", {})
    env = doc.get("environment", {})
    agent = doc.get("agent", {})
    verifier = doc.get("verifier", {})

    verifiers = load_verifiers(folder / "tests" / "verifiers.jsonl")

    seeds_dir = folder / "environment" / "seeds"
    seed_files = sorted(p.name for p in seeds_dir.glob("*.sql")) if seeds_dir.exists() else []

    return {
        "instance_id": folder.name,
        "domain": domain_label(meta.get("category", "")),
        "difficulty": meta.get("difficulty", ""),
        "task": task.get("description", ""),
        "golden_end_state": f"{len(verifiers)} checkpoints",
        "model_a_score": None,
        "model_b_score": None,
        "source_task_id": meta.get("source_task_id", ""),
        "keywords": meta.get("keywords", []),
        "actor": {
            "name": actor.get("name", ""),
            "email": actor.get("email", ""),
            "user_id": actor.get("user_id", ""),
        },
        "gyms": meta.get("gyms", []),
        "gym_contexts": meta.get("gym_contexts", {}),
        "selected_tools": meta.get("selected_tools", []),
        "restricted_tools": meta.get("restricted_tools", []),
        "verifiers": verifiers,
        "seed_files": seed_files,
        "environment": {
            "build_timeout_sec": env.get("build_timeout_sec"),
            "cpus": env.get("cpus"),
            "memory_mb": env.get("memory_mb"),
            "storage_mb": env.get("storage_mb"),
            "network_mode": env.get("network_mode"),
            "allowed_hosts": env.get("allowed_hosts", []),
            "mcp_servers": env.get("mcp_servers", []),
        },
        "agent": {
            "timeout_sec": agent.get("timeout_sec"),
            "network_mode": agent.get("network_mode"),
            "allowed_hosts": agent.get("allowed_hosts", []),
        },
        "verifier_timeout_sec": verifier.get("timeout_sec"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC,
                        help=f"Source folder of UUID task dirs (default: {DEFAULT_SRC})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"Output JSON path (default: {DEFAULT_OUT})")
    args = parser.parse_args()

    src: Path = args.src
    if not src.is_dir():
        print(f"error: source folder not found: {src}", file=sys.stderr)
        return 2

    folders = [p for p in sorted(src.iterdir()) if p.is_dir() and (p / "task.toml").exists()]
    if not folders:
        print(f"error: no task folders with task.toml in {src}", file=sys.stderr)
        return 2

    records = [build_record(p) for p in folders]
    records.sort(key=lambda r: r["instance_id"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"wrote {len(records)} records to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
