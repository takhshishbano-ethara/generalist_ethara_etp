from __future__ import annotations

import hashlib


def normalize(raw: str) -> str:
    if not raw:
        return ""
    return " ".join(raw.strip().lower().split())


def content_hash(raw: str) -> str:
    return hashlib.sha256(normalize(raw).encode("utf-8")).hexdigest()


def is_duplicate(env, raw_content: str) -> bool:
    digest = content_hash(raw_content)
    return bool(env["lynceus.history"].sudo().search_count([("content_hash", "=", digest)]))


def record_rejection(env, raw_content: str, batch_id: int | None = None) -> None:
    digest = content_hash(raw_content)
    History = env["lynceus.history"].sudo()
    existing = History.search([("content_hash", "=", digest)], limit=1)
    if existing:
        return
    History.create({
        "content_hash": digest,
        "batch_id": batch_id or False,
        "rejected_as_duplicate": True,
    })
