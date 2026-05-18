"""
Phase 4E: Network & API Interception.

Captures all network requests during page load to detect:
- API endpoints and patterns
- CMS CDN signatures
- Asset loading patterns
- WebSocket connections
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from urllib.parse import urlparse

from playwright.async_api import Page, Response

logger = logging.getLogger(__name__)

CMS_CDN_PATTERNS = {
    "sanity": ["cdn.sanity.io", "apicdn.sanity.io"],
    "contentful": ["ctfassets.net", "cdn.contentful.com", "images.ctfassets.net"],
    "prismic": ["prismic.io", "cdn.prismic.io"],
    "datocms": ["datocms-assets.com", "dato-cms-assets.com"],
    "storyblok": ["storyblok.com", "a.storyblok.com"],
    "hygraph": ["graphcms.com", "media.graphcms.com", "hygraph.com"],
    "strapi": ["strapi.io"],
    "cloudinary": ["res.cloudinary.com"],
    "imgix": ["imgix.net"],
    "mux": ["mux.com", "stream.mux.com"],
    "vercel": ["vercel.com", "_vercel"],
    "netlify": ["netlify.app", "netlify.com"],
    "cloudflare": ["cdnjs.cloudflare.com", "cdn-cgi"],
    "akamai": ["akamaized.net", "akamaihd.net"],
    "fastly": ["fastly.net", "fastly.com"],
    "aws_s3": ["s3.amazonaws.com", "s3-", ".amazonaws.com"],
    "firebase": ["firebasestorage.googleapis.com", "firebaseio.com"],
}


async def analyze_network(page: Page, url: str) -> dict:
    """
    Intercept network traffic and analyze API patterns, CDN usage, and asset loading.
    Must be called BEFORE page.goto() to capture all requests.
    """
    requests_log: list[dict] = []
    websockets: list[dict] = []
    api_endpoints: list[dict] = []
    cms_detected: dict = {}
    cdn_detected: dict = {}
    asset_stats = defaultdict(lambda: {"count": 0, "total_size": 0, "urls": []})

    base_domain = urlparse(url).netloc.replace("www.", "")

    async def _on_response(response: Response):
        try:
            req = response.request
            resp_url = response.url
            status = response.status
            content_type = response.headers.get("content-type", "")
            content_length = int(response.headers.get("content-length", 0))
            parsed = urlparse(resp_url)
            domain = parsed.netloc

            entry = {
                "url": resp_url[:300],
                "method": req.method,
                "status": status,
                "content_type": content_type.split(";")[0].strip(),
                "size": content_length,
                "domain": domain,
                "is_third_party": base_domain not in domain,
            }

            # Categorize by type
            ct = content_type.lower()
            if "json" in ct or "graphql" in ct:
                entry["category"] = "api"
                api_endpoints.append({
                    "url": resp_url[:300],
                    "method": req.method,
                    "status": status,
                    "path": parsed.path,
                    "domain": domain,
                })
            elif "javascript" in ct or "ecmascript" in ct:
                entry["category"] = "script"
                asset_stats["scripts"]["count"] += 1
                asset_stats["scripts"]["total_size"] += content_length
            elif "css" in ct:
                entry["category"] = "stylesheet"
                asset_stats["stylesheets"]["count"] += 1
                asset_stats["stylesheets"]["total_size"] += content_length
            elif ct.startswith("image/"):
                entry["category"] = "image"
                asset_stats["images"]["count"] += 1
                asset_stats["images"]["total_size"] += content_length
                if len(asset_stats["images"].get("urls", [])) < 10:
                    asset_stats["images"]["urls"].append(resp_url[:200])
            elif ct.startswith("font/") or "font" in ct:
                entry["category"] = "font"
                asset_stats["fonts"]["count"] += 1
                asset_stats["fonts"]["total_size"] += content_length
            elif ct.startswith("video/"):
                entry["category"] = "video"
                asset_stats["videos"]["count"] += 1
                asset_stats["videos"]["total_size"] += content_length
            else:
                entry["category"] = "other"

            requests_log.append(entry)

            # CMS/CDN detection
            for name, patterns in CMS_CDN_PATTERNS.items():
                for pattern in patterns:
                    if pattern in domain or pattern in resp_url:
                        if name not in cms_detected:
                            cms_detected[name] = {"urls": [], "count": 0}
                        cms_detected[name]["count"] += 1
                        if len(cms_detected[name]["urls"]) < 3:
                            cms_detected[name]["urls"].append(resp_url[:200])

        except Exception as e:
            logger.debug("Response handler error: %s", e)

    page.on("response", _on_response)

    # WebSocket detection
    page.on("websocket", lambda ws: websockets.append({
        "url": ws.url[:300],
    }))

    return {
        "requests_log": requests_log,
        "api_endpoints": api_endpoints,
        "websockets": websockets,
        "cms_detected": cms_detected,
        "asset_stats": dict(asset_stats),
    }


def summarize_network(raw_data: dict) -> dict:
    """Post-process network data into a summary for PRD generation."""
    requests = raw_data.get("requests_log", [])
    apis = raw_data.get("api_endpoints", [])
    cms = raw_data.get("cms_detected", {})
    assets = raw_data.get("asset_stats", {})
    websockets = raw_data.get("websockets", [])

    total_requests = len(requests)
    total_size = sum(r.get("size", 0) for r in requests)
    third_party = [r for r in requests if r.get("is_third_party")]

    # Infer data model from API endpoints
    api_patterns = _infer_api_patterns(apis)

    # Detect hosting/CDN
    hosting = []
    for name in ["vercel", "netlify", "cloudflare", "aws_s3", "firebase"]:
        if name in cms:
            hosting.append(name)

    # Separate CMS from CDN/hosting
    cms_names = []
    cdn_names = []
    for name in cms:
        if name in ("vercel", "netlify", "cloudflare", "akamai", "fastly", "aws_s3", "firebase"):
            cdn_names.append(name)
        elif name in ("cloudinary", "imgix", "mux"):
            cdn_names.append(name)
        else:
            cms_names.append(name)

    return {
        "total_requests": total_requests,
        "total_transfer_size_kb": round(total_size / 1024, 1),
        "third_party_requests": len(third_party),
        "third_party_domains": list(set(r.get("domain", "") for r in third_party))[:10],
        "api_endpoints": apis[:20],
        "api_patterns": api_patterns,
        "cms_detected": cms_names,
        "cdn_detected": cdn_names,
        "hosting_detected": hosting,
        "asset_stats": assets,
        "websocket_count": len(websockets),
        "websocket_urls": [ws["url"] for ws in websockets[:5]],
    }


def _infer_api_patterns(apis: list[dict]) -> list[dict]:
    """Group API calls into patterns for data model inference."""
    path_groups: dict[str, list] = defaultdict(list)

    for api in apis:
        path = api.get("path", "")
        # Normalize: remove IDs from paths
        normalized = re.sub(r'/[0-9a-f-]{20,}', '/{id}', path)
        normalized = re.sub(r'/\d+', '/{id}', normalized)
        path_groups[normalized].append(api)

    patterns = []
    for pattern, calls in path_groups.items():
        if len(pattern) < 3:
            continue
        # Infer entity name from path
        parts = [p for p in pattern.split("/") if p and p != "{id}"]
        entity = parts[-1] if parts else "unknown"

        patterns.append({
            "pattern": pattern,
            "entity": entity,
            "methods": list(set(c.get("method", "GET") for c in calls)),
            "call_count": len(calls),
            "example_url": calls[0].get("url", "")[:200],
        })

    return sorted(patterns, key=lambda x: x["call_count"], reverse=True)[:15]
