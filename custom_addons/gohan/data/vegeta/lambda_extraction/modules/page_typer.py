"""
Page typer -- classifies each crawled URL into a Vegeta page-type bucket.

Pure-Python, no browser. Consumes the route list produced by site_discoverer
plus optional network_data (to spot pages that hit JSON APIs) and produces
the `observed_pages` array the PRD bundle needs.

The vegeta-prd-gen.md `observed_pages` field expects, per page:
    route, page_type, visible_fields, filters, flows, reference_screenshot_id.

Without an extra DOM pass per page we can only fill route and page_type
deterministically. visible_fields / filters / flows are left as URL-derived
hints (path_segments, query_params, entity_hint); the PRD generator treats
those as Tier 2 evidence, not Tier 1, and fills in field detail from the
category emphasis table and other bundle fields (schema_org_entities,
openapi_specs, form_fields).

page_type vocabulary (matches the vegeta-prd-gen.md spec, line 21):
    landing, listing, detail, search, auth, dashboard, docs, pricing,
    settings, checkout, about, legal, other.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, parse_qsl

from config import PAGE_TYPE_HINTS

_PAGE_TYPE_ORDER = [
    "auth",
    "checkout",
    "pricing",
    "search",
    "detail",
    "listing",
    "dashboard",
    "docs",
    "settings",
    "legal",
    "about",
    "landing",
]

_COMPILED_HINTS: dict[str, list[re.Pattern[str]]] = {
    page_type: [re.compile(p, re.IGNORECASE) for p in patterns]
    for page_type, patterns in PAGE_TYPE_HINTS.items()
}


def type_pages(
    pages: list[Any],
    style_data: dict | None = None,
    network_data: dict | None = None,
) -> list[dict[str, Any]]:
    """Classify each page URL and emit the observed_pages bundle.

    Args:
        pages: list of page URLs (str) or page dicts containing 'url'. Both
            forms accepted because site_discoverer returns strings while
            future enrichments may pass dicts.
        style_data: unused for now; reserved for future DOM-derived fields.
        network_data: optional summarize_network() output. If present, pages
            whose path appears in api_patterns are flagged with a
            'hits_json_api' signal -- useful for distinguishing dashboards
            from static pages.

    Returns:
        [
            {
                "url": str,
                "route": str,             -- path with query stripped
                "page_type": str,         -- one of the 13 buckets above
                "path_segments": [str],
                "query_params": [str],    -- list of param names only
                "entity_hint": str|None,  -- inferred noun ('product', 'course', ...)
                "signals": [str],         -- 'has_query_params', 'hits_json_api',
                                            'has_id_segment'
            },
            ...
        ]
    """
    typed: list[dict[str, Any]] = []
    seen_routes: set[str] = set()

    api_pattern_paths = _collect_api_paths(network_data)

    for entry in pages or []:
        url = _coerce_url(entry)
        if not url:
            continue
        parsed = urlparse(url)
        path = parsed.path or "/"
        route = path.rstrip("/") or "/"
        if route in seen_routes:
            continue
        seen_routes.add(route)

        page_type = _classify_path(path)
        segments = [s for s in path.split("/") if s]
        query_params = sorted({k for k, _ in parse_qsl(parsed.query)})

        signals: list[str] = []
        if query_params:
            signals.append("has_query_params")
        if _has_id_segment(segments):
            signals.append("has_id_segment")
            if page_type == "listing":
                page_type = "detail"
        if _path_in_api(path, api_pattern_paths):
            signals.append("hits_json_api")

        typed.append({
            "url": url,
            "route": route,
            "page_type": page_type,
            "path_segments": segments,
            "query_params": query_params,
            "entity_hint": _infer_entity(segments, page_type),
            "signals": signals,
        })

    return typed


def _coerce_url(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        for key in ("url", "href", "loc"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _classify_path(path: str) -> str:
    normalized = path.rstrip("/") or "/"
    if normalized == "/" or normalized.lower() in {"/home", "/index"}:
        return "landing"
    for page_type in _PAGE_TYPE_ORDER:
        for pattern in _COMPILED_HINTS.get(page_type, []):
            if pattern.search(normalized):
                return page_type
    return "other"


_ID_SEGMENT_RE = re.compile(
    r"^("
    r"\d+"
    r"|[0-9a-f]{8,}"
    r"|[0-9a-f]{4,}-[0-9a-f]{4,}"
    r"|[A-Za-z0-9_-]{12,}"
    r")$"
)


def _has_id_segment(segments: list[str]) -> bool:
    return any(_ID_SEGMENT_RE.match(seg) for seg in segments)


def _infer_entity(segments: list[str], page_type: str) -> str | None:
    if not segments:
        return None
    nouns = [s for s in segments if not _ID_SEGMENT_RE.match(s)]
    if not nouns:
        return None
    if page_type == "detail" and len(nouns) >= 1:
        candidate = nouns[-1] if len(nouns) == len(segments) else nouns[-1]
        if len(nouns) >= 2 and _has_id_segment(segments):
            candidate = nouns[-1]
        return _singularize(candidate)
    if page_type in {"listing", "search"}:
        return _singularize(nouns[-1])
    return _singularize(nouns[-1])


def _singularize(noun: str) -> str:
    n = noun.lower()
    if n.endswith("ies") and len(n) > 3:
        return n[:-3] + "y"
    if n.endswith("ses") or n.endswith("xes") or n.endswith("zes"):
        return n[:-2]
    if n.endswith("s") and not n.endswith("ss") and len(n) > 3:
        return n[:-1]
    return n


def _collect_api_paths(network_data: dict | None) -> set[str]:
    if not network_data:
        return set()
    paths: set[str] = set()
    for pat in network_data.get("api_patterns", []) or []:
        pattern = pat.get("pattern") if isinstance(pat, dict) else None
        if isinstance(pattern, str):
            paths.add(pattern)
    for ep in network_data.get("api_endpoints", []) or []:
        path = ep.get("path") if isinstance(ep, dict) else None
        if isinstance(path, str):
            paths.add(path)
    return paths


def _path_in_api(path: str, api_paths: set[str]) -> bool:
    if not api_paths:
        return False
    normalized = re.sub(r"/\d+", "/{id}", path)
    normalized = re.sub(r"/[0-9a-f-]{20,}", "/{id}", normalized)
    return normalized in api_paths or path in api_paths
