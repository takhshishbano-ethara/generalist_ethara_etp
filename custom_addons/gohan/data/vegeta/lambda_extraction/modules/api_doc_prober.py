"""
API documentation prober.

Probes the target host for machine-readable API schemas and protocol descriptors:
    OpenAPI / Swagger JSON+YAML at conventional paths, GraphQL introspection,
    robots.txt, sitemap.xml + sitemap index, /.well-known/* manifests.

Output feeds Sections 6 (Data Model) and 7 (API Design) of the Vegeta PRD with
ground-truth entity shapes and endpoint definitions that the model would
otherwise have to invent.

All probes use page.request.get() (Playwright APIRequestContext) with a tight
timeout. A 404/timeout per path is the expected case for most sites; the prober
returns whatever it finds and never raises.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


_PROBE_TIMEOUT_MS = 8_000
_MAX_BODY_BYTES = 512 * 1024


GRAPHQL_INTROSPECTION_QUERY = """
{
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types {
      kind
      name
      description
      fields(includeDeprecated: false) {
        name
        description
        type { kind name ofType { kind name ofType { kind name } } }
        args { name type { kind name ofType { kind name } } }
      }
      inputFields { name type { kind name ofType { kind name } } }
      interfaces { name }
      enumValues(includeDeprecated: false) { name }
      possibleTypes { name }
    }
  }
}
""".strip()


async def probe_api_docs(page, url: str) -> dict[str, Any]:
    """Probe a host for API documentation and protocol descriptors.

    Args:
        page: Playwright Page bound to a context with a live request handle.
        url: The target URL (any path; the prober uses scheme+host only).

    Returns:
        {
            "openapi_specs": [{path, version, title, server_urls[], endpoint_count, entities[], endpoints[]}, ...],
            "graphql_schemas": [{path, query_type, mutation_type, type_count, types[{name, kind, fields[{name, type}]}]}, ...],
            "robots_txt": {found, user_agents[], disallowed_paths[], sitemaps[]} or None,
            "sitemaps": {urls_found[], total_urls},
            "well_known": {paths_found[]},
            "probe_log": [{path, status, content_type, found}, ...]
        }
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    result: dict[str, Any] = {
        "openapi_specs": [],
        "graphql_schemas": [],
        "robots_txt": None,
        "sitemaps": {"urls_found": [], "total_urls": 0},
        "well_known": {"paths_found": []},
        "probe_log": [],
    }

    from config import API_DOC_PROBE_PATHS

    for probe_path in API_DOC_PROBE_PATHS:
        full_url = urljoin(base + "/", probe_path.lstrip("/"))
        log_entry = {"path": probe_path, "status": None, "content_type": None, "found": False}

        try:
            response = await page.request.get(full_url, timeout=_PROBE_TIMEOUT_MS, max_redirects=2)
            log_entry["status"] = response.status
            headers = await response.all_headers()
            content_type = (headers.get("content-type") or "").lower()
            log_entry["content_type"] = content_type[:80]

            if response.status != 200:
                result["probe_log"].append(log_entry)
                continue

            body_bytes = await response.body()
            if len(body_bytes) > _MAX_BODY_BYTES:
                body_bytes = body_bytes[:_MAX_BODY_BYTES]
            body_text = body_bytes.decode("utf-8", errors="replace")

            if probe_path in ("/graphql", "/api/graphql", "/query", "/api/query"):
                introspection = await _attempt_graphql_introspection(page, full_url)
                if introspection:
                    introspection["path"] = probe_path
                    result["graphql_schemas"].append(introspection)
                    log_entry["found"] = True
            elif "openapi" in probe_path or "swagger" in probe_path or "api-docs" in probe_path:
                spec = _parse_openapi(body_text, content_type)
                if spec:
                    spec["path"] = probe_path
                    result["openapi_specs"].append(spec)
                    log_entry["found"] = True
            elif probe_path.startswith("/.well-known/"):
                result["well_known"]["paths_found"].append({"path": probe_path, "body_sample": body_text[:1024]})
                log_entry["found"] = True

        except Exception as exc:
            log_entry["status"] = f"error: {type(exc).__name__}"

        result["probe_log"].append(log_entry)

    result["robots_txt"] = await _probe_robots(page, base)
    result["sitemaps"] = await _probe_sitemaps(page, base, result["robots_txt"])

    return result


def _parse_openapi(body_text: str, content_type: str) -> dict[str, Any] | None:
    spec_dict: Any = None
    try:
        if "json" in content_type or body_text.lstrip().startswith("{"):
            spec_dict = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(spec_dict, dict):
        return None

    version = spec_dict.get("openapi") or spec_dict.get("swagger")
    if not version:
        return None

    info = spec_dict.get("info") or {}
    title = info.get("title", "")

    server_urls: list[str] = []
    for s in spec_dict.get("servers", []) or []:
        if isinstance(s, dict) and s.get("url"):
            server_urls.append(s["url"])
    if not server_urls and spec_dict.get("host"):
        scheme = (spec_dict.get("schemes") or ["https"])[0]
        base_path = spec_dict.get("basePath", "")
        server_urls.append(f"{scheme}://{spec_dict['host']}{base_path}")

    paths = spec_dict.get("paths") or {}
    endpoints: list[dict[str, Any]] = []
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            op = ops.get(method)
            if not isinstance(op, dict):
                continue
            endpoints.append({
                "method": method.upper(),
                "path": path,
                "summary": (op.get("summary") or "")[:120],
                "operation_id": op.get("operationId", ""),
                "tags": op.get("tags", []),
            })

    schemas = (spec_dict.get("components") or {}).get("schemas") or spec_dict.get("definitions") or {}
    entities: list[dict[str, Any]] = []
    for name, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        props = schema.get("properties") or {}
        if not props:
            continue
        fields = []
        for field_name, field_schema in list(props.items())[:40]:
            if not isinstance(field_schema, dict):
                continue
            ftype = field_schema.get("type") or field_schema.get("$ref", "ref")
            fmt = field_schema.get("format")
            fields.append({
                "name": field_name,
                "type": ftype,
                "format": fmt,
                "required": field_name in (schema.get("required") or []),
            })
        entities.append({"name": name, "fields": fields[:40]})

    return {
        "version": version,
        "title": title,
        "server_urls": server_urls[:5],
        "endpoint_count": len(endpoints),
        "entities": entities[:40],
        "endpoints": endpoints[:80],
    }


async def _attempt_graphql_introspection(page, endpoint_url: str) -> dict[str, Any] | None:
    try:
        response = await page.request.post(
            endpoint_url,
            timeout=_PROBE_TIMEOUT_MS,
            data=json.dumps({"query": GRAPHQL_INTROSPECTION_QUERY}),
            headers={"content-type": "application/json"},
        )
        if response.status != 200:
            return None
        body = await response.text()
        data = json.loads(body)
    except Exception:
        return None

    schema = ((data or {}).get("data") or {}).get("__schema")
    if not isinstance(schema, dict):
        return None

    types_raw = schema.get("types") or []
    types_out: list[dict[str, Any]] = []
    for t in types_raw:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or ""
        if not name or name.startswith("__"):
            continue
        fields_raw = t.get("fields") or t.get("inputFields") or []
        if not fields_raw and t.get("kind") not in ("ENUM",):
            continue
        fields_out = []
        for f in fields_raw[:30]:
            if not isinstance(f, dict):
                continue
            ftype = _unwrap_graphql_type(f.get("type") or {})
            fields_out.append({"name": f.get("name", ""), "type": ftype})
        types_out.append({
            "name": name,
            "kind": t.get("kind", ""),
            "fields": fields_out,
        })

    return {
        "query_type": (schema.get("queryType") or {}).get("name"),
        "mutation_type": (schema.get("mutationType") or {}).get("name"),
        "subscription_type": (schema.get("subscriptionType") or {}).get("name"),
        "type_count": len(types_out),
        "types": types_out[:40],
    }


def _unwrap_graphql_type(t: dict[str, Any]) -> str:
    kind = t.get("kind", "")
    name = t.get("name")
    if name:
        return name
    of_type = t.get("ofType")
    if isinstance(of_type, dict):
        inner = _unwrap_graphql_type(of_type)
        if kind == "LIST":
            return f"[{inner}]"
        if kind == "NON_NULL":
            return f"{inner}!"
        return inner
    return kind or "unknown"


async def _probe_robots(page, base: str) -> dict[str, Any] | None:
    try:
        response = await page.request.get(f"{base}/robots.txt", timeout=_PROBE_TIMEOUT_MS)
        if response.status != 200:
            return None
        body = await response.text()
    except Exception:
        return None

    if len(body) > _MAX_BODY_BYTES:
        body = body[:_MAX_BODY_BYTES]

    user_agents: list[str] = []
    disallowed: list[str] = []
    sitemaps: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == "user-agent" and value not in user_agents:
            user_agents.append(value)
        elif key == "disallow":
            disallowed.append(value)
        elif key == "sitemap":
            sitemaps.append(value)

    return {
        "found": True,
        "user_agents": user_agents[:20],
        "disallowed_paths": list(dict.fromkeys(disallowed))[:40],
        "sitemaps": sitemaps[:10],
    }


_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)


async def _probe_sitemaps(page, base: str, robots: dict[str, Any] | None) -> dict[str, Any]:
    candidates: list[str] = []
    if robots and robots.get("sitemaps"):
        candidates.extend(robots["sitemaps"])
    else:
        candidates.extend([f"{base}/sitemap.xml", f"{base}/sitemap_index.xml", f"{base}/sitemap-index.xml"])

    visited: set[str] = set()
    all_urls: list[str] = []

    for sitemap_url in candidates[:5]:
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        try:
            response = await page.request.get(sitemap_url, timeout=_PROBE_TIMEOUT_MS)
            if response.status != 200:
                continue
            body = await response.text()
        except Exception:
            continue

        if len(body) > _MAX_BODY_BYTES * 4:
            body = body[:_MAX_BODY_BYTES * 4]

        locs = _LOC_RE.findall(body)
        if "<sitemapindex" in body.lower():
            for child in locs[:10]:
                if child in visited or len(visited) > 8:
                    continue
                visited.add(child)
                try:
                    child_resp = await page.request.get(child, timeout=_PROBE_TIMEOUT_MS)
                    if child_resp.status != 200:
                        continue
                    child_body = await child_resp.text()
                except Exception:
                    continue
                if len(child_body) > _MAX_BODY_BYTES * 4:
                    child_body = child_body[:_MAX_BODY_BYTES * 4]
                all_urls.extend(_LOC_RE.findall(child_body))
        else:
            all_urls.extend(locs)

    deduped = list(dict.fromkeys(all_urls))
    return {
        "urls_found": deduped[:200],
        "total_urls": len(deduped),
    }
