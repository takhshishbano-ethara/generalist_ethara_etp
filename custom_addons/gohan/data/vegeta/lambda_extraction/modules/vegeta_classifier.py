"""
Vegeta business-category classifier.

Maps the collected scrape signals to exactly one of the 16 fixed Vegeta
business categories (Public Utility, News, Publishing, Retail, Services, ERP,
Knowledge, Procurement, Vertical Markets, HCM, CRM, Gov. Portal, Community,
TMS, Multimedia, AI Platform).

Signal sources and their weights, strongest first:
    schema.org types from JSON-LD .... 5  (strongest -- explicit declaration)
    URL path patterns ................ 2  (per matching route)
    business_signals billing model ... 2  (per-seat, transaction-fee, etc.)
    domain TLD ....................... 3  (.gov / .edu / .mil only)
    tech_signals integrations ........ 1  (Shopify, Stripe, Mux, LearnDash, ...)

Returns a dict with the chosen category, the per-category score map, and the
evidence list that drove the decision. The category string is guaranteed to
be one of the 16 in config.VEGETA_CATEGORIES.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


_SCHEMA_ORG_TO_CATEGORY: dict[str, tuple[str, int]] = {
    "course": ("Knowledge", 5),
    "courseinstance": ("Knowledge", 5),
    "learningresource": ("Knowledge", 5),
    "educationaloccupationalprogram": ("Knowledge", 5),
    "quiz": ("Knowledge", 4),
    "product": ("Retail", 5),
    "productgroup": ("Retail", 5),
    "offer": ("Retail", 3),
    "aggregateoffer": ("Retail", 4),
    "individualproduct": ("Retail", 5),
    "newsarticle": ("News", 5),
    "newspaper": ("News", 5),
    "reportagenewsarticle": ("News", 5),
    "blogposting": ("Publishing", 4),
    "blog": ("Publishing", 3),
    "article": ("Publishing", 2),
    "jobposting": ("Vertical Markets", 4),
    "event": ("Services", 4),
    "businessevent": ("Services", 3),
    "lodgingbusiness": ("Vertical Markets", 5),
    "hotel": ("Vertical Markets", 5),
    "vacationrental": ("Vertical Markets", 5),
    "accommodation": ("Vertical Markets", 4),
    "rentalcarreservation": ("Vertical Markets", 4),
    "vehicle": ("Vertical Markets", 3),
    "realestatelisting": ("Vertical Markets", 5),
    "apartmentcomplex": ("Vertical Markets", 4),
    "governmentservice": ("Gov. Portal", 5),
    "governmentorganization": ("Gov. Portal", 5),
    "governmentpermit": ("Gov. Portal", 5),
    "civicstructure": ("Gov. Portal", 3),
    "recipe": ("Community", 3),
    "howto": ("Community", 3),
    "qapage": ("Community", 4),
    "discussionforumposting": ("Community", 5),
    "comment": ("Community", 2),
    "videoobject": ("Multimedia", 4),
    "musicrecording": ("Multimedia", 5),
    "musicalbum": ("Multimedia", 5),
    "podcastepisode": ("Multimedia", 5),
    "podcastseries": ("Multimedia", 5),
    "tvepisode": ("Multimedia", 5),
    "movie": ("Multimedia", 4),
    "softwareapplication": ("AI Platform", 1),
    "webapi": ("AI Platform", 2),
}


_PATH_HINTS: list[tuple[str, str, int]] = [
    ("/courses", "Knowledge", 3),
    ("/course/", "Knowledge", 3),
    ("/lesson", "Knowledge", 3),
    ("/lessons", "Knowledge", 3),
    ("/enroll", "Knowledge", 3),
    ("/certificate", "Knowledge", 2),
    ("/quiz", "Knowledge", 2),
    ("/syllabus", "Knowledge", 2),
    ("/products", "Retail", 3),
    ("/product/", "Retail", 3),
    ("/cart", "Retail", 4),
    ("/checkout", "Retail", 4),
    ("/shop", "Retail", 3),
    ("/store", "Retail", 2),
    ("/collection", "Retail", 2),
    ("/order", "Retail", 2),
    ("/return", "Retail", 1),
    ("/articles", "News", 2),
    ("/news", "News", 3),
    ("/breaking", "News", 4),
    ("/section/", "News", 2),
    ("/topic/", "News", 1),
    ("/posts", "Publishing", 2),
    ("/post/", "Publishing", 2),
    ("/blog", "Publishing", 2),
    ("/newsletter", "Publishing", 3),
    ("/subscribe", "Publishing", 1),
    ("/issue", "Publishing", 2),
    ("/jobs", "Vertical Markets", 3),
    ("/job/", "Vertical Markets", 3),
    ("/careers", "Vertical Markets", 1),
    ("/book", "Services", 2),
    ("/booking", "Services", 3),
    ("/appointment", "Services", 4),
    ("/reserve", "Services", 2),
    ("/availability", "Services", 3),
    ("/listing", "Vertical Markets", 3),
    ("/listings", "Vertical Markets", 3),
    ("/property", "Vertical Markets", 3),
    ("/properties", "Vertical Markets", 3),
    ("/rentals", "Vertical Markets", 3),
    ("/host", "Vertical Markets", 2),
    ("/payout", "Vertical Markets", 2),
    ("/contacts", "CRM", 3),
    ("/companies", "CRM", 3),
    ("/deals", "CRM", 4),
    ("/pipeline", "CRM", 4),
    ("/sequences", "CRM", 3),
    ("/leads", "CRM", 3),
    ("/employees", "HCM", 4),
    ("/payroll", "HCM", 5),
    ("/time-off", "HCM", 4),
    ("/timesheet", "HCM", 3),
    ("/onboarding", "HCM", 2),
    ("/org-chart", "HCM", 4),
    ("/projects", "TMS", 2),
    ("/tasks", "TMS", 3),
    ("/board", "TMS", 2),
    ("/boards", "TMS", 3),
    ("/kanban", "TMS", 4),
    ("/sprint", "TMS", 3),
    ("/suppliers", "Procurement", 4),
    ("/rfq", "Procurement", 5),
    ("/bids", "Procurement", 4),
    ("/purchase-order", "Procurement", 4),
    ("/contracts", "Procurement", 2),
    ("/approval", "Procurement", 1),
    ("/modules", "ERP", 1),
    ("/ledger", "ERP", 4),
    ("/journal", "ERP", 2),
    ("/invoices", "ERP", 2),
    ("/agencies", "Gov. Portal", 3),
    ("/file-claim", "Gov. Portal", 4),
    ("/citizen", "Gov. Portal", 4),
    ("/permit", "Gov. Portal", 3),
    ("/license", "Gov. Portal", 2),
    ("/thread", "Community", 3),
    ("/forum", "Community", 4),
    ("/community", "Community", 2),
    ("/comments", "Community", 2),
    ("/r/", "Community", 3),
    ("/watch", "Multimedia", 4),
    ("/play", "Multimedia", 2),
    ("/listen", "Multimedia", 3),
    ("/channel", "Multimedia", 3),
    ("/album", "Multimedia", 3),
    ("/podcast", "Multimedia", 3),
    ("/episode", "Multimedia", 3),
    ("/video/", "Multimedia", 3),
    ("/track/", "Multimedia", 3),
    ("/playground", "AI Platform", 5),
    ("/models", "AI Platform", 4),
    ("/api-keys", "AI Platform", 4),
    ("/usage", "AI Platform", 2),
    ("/completions", "AI Platform", 4),
    ("/embeddings", "AI Platform", 4),
    ("/bill-pay", "Public Utility", 4),
    ("/utilities", "Public Utility", 3),
    ("/water", "Public Utility", 2),
    ("/electric", "Public Utility", 2),
    ("/service-request", "Public Utility", 2),
    ("/account-lookup", "Public Utility", 3),
]


_BILLING_HINT_TO_CATEGORY: dict[str, list[tuple[str, int]]] = {
    "transaction-fee": [("Vertical Markets", 4), ("Retail", 1)],
    "advertising-supported": [("News", 3), ("Multimedia", 2), ("Community", 2), ("Publishing", 1)],
    "per-seat": [("CRM", 1), ("ERP", 1), ("HCM", 1), ("TMS", 1), ("Procurement", 1), ("AI Platform", 1)],
    "usage-based": [("AI Platform", 4)],
    "government-funded": [("Gov. Portal", 4), ("Public Utility", 3)],
    "free-tier": [],
    "enterprise-contact-sales": [("ERP", 1), ("CRM", 1), ("HCM", 1), ("Procurement", 1), ("AI Platform", 1)],
}


_TLD_TO_CATEGORY: list[tuple[str, str, int]] = [
    (".gov", "Gov. Portal", 5),
    (".mil", "Gov. Portal", 4),
    (".edu", "Knowledge", 4),
    (".gov.uk", "Gov. Portal", 5),
    (".gov.in", "Gov. Portal", 5),
    (".gov.au", "Gov. Portal", 5),
    (".gov.ca", "Gov. Portal", 5),
    (".ac.uk", "Knowledge", 4),
]


_TECH_TO_CATEGORY: dict[str, list[tuple[str, int]]] = {
    "shopify": [("Retail", 4)],
    "woocommerce": [("Retail", 4)],
    "bigcommerce": [("Retail", 4)],
    "magento": [("Retail", 4)],
    "stripe": [("Retail", 1), ("Services", 1), ("Vertical Markets", 1), ("Publishing", 1)],
    "mux": [("Multimedia", 3)],
    "cloudflare stream": [("Multimedia", 3)],
    "vimeo": [("Multimedia", 2)],
    "ghost": [("Publishing", 4)],
    "substack": [("Publishing", 4)],
    "beehiiv": [("Publishing", 4)],
    "wordpress": [("Publishing", 2), ("News", 1)],
    "learndash": [("Knowledge", 4)],
    "thinkific": [("Knowledge", 4)],
    "teachable": [("Knowledge", 4)],
    "moodle": [("Knowledge", 4)],
    "canvas": [("Knowledge", 3)],
    "discourse": [("Community", 5)],
    "vanilla forums": [("Community", 4)],
    "salesforce": [("CRM", 3)],
    "hubspot": [("CRM", 3)],
    "intercom": [("CRM", 1), ("Services", 1)],
    "workday": [("HCM", 4)],
    "bamboohr": [("HCM", 4)],
    "gusto": [("HCM", 3)],
    "jira": [("TMS", 4)],
    "linear": [("TMS", 4)],
    "asana": [("TMS", 4)],
    "monday.com": [("TMS", 4)],
    "trello": [("TMS", 3)],
    "coupa": [("Procurement", 4)],
    "ariba": [("Procurement", 4)],
    "netsuite": [("ERP", 4)],
    "sap": [("ERP", 3)],
    "oracle erp": [("ERP", 4)],
    "openai": [("AI Platform", 3)],
    "anthropic": [("AI Platform", 3)],
    "huggingface": [("AI Platform", 4)],
    "replicate": [("AI Platform", 4)],
}


def classify_vegeta_category(
    site_data: dict | None,
    network_data: dict | None,
    business_signals: dict | None,
    schema_org_types: list[str] | None,
    observed_routes: list[str] | None,
    api_doc_data: dict | None = None,
) -> dict[str, Any]:
    """Map collected signals to one of the 16 fixed Vegeta business categories.

    Args:
        site_data: Output of site_discoverer (has url, title, description,
            tech_stack, cms, pages).
        network_data: Output of summarize_network() (has api_patterns,
            api_endpoints, cdn_detected, cms_detected).
        business_signals: Output of extract_business_signals() (has tiers,
            billing_model_hints, free_tier_present, enterprise_contact_only).
        schema_org_types: Flat list of schema.org @type strings observed in
            JSON-LD, lowercased. Source: style_extractor.seo.json_ld.
        observed_routes: Flat list of route URLs/paths from site_data['pages']
            plus any other route source.
        api_doc_data: Optional output of probe_api_docs() (has openapi_specs,
            graphql_schemas). Used as a weak SaaS hint.

    Returns:
        {
            "category": str,                   -- one of config.VEGETA_CATEGORIES
            "confidence": "high"|"medium"|"low",
            "scores": {category_name: float, ...},
            "evidence": [{source, signal, category, weight}, ...],
            "runner_up": str or None,
        }
    """
    try:
        from config import VEGETA_CATEGORIES
    except ImportError:
        VEGETA_CATEGORIES = []

    scores: dict[str, float] = {cat: 0.0 for cat in VEGETA_CATEGORIES}
    evidence: list[dict[str, Any]] = []

    def _vote(category: str, weight: int, source: str, signal: str) -> None:
        if category not in scores:
            return
        scores[category] += weight
        evidence.append({"source": source, "signal": signal, "category": category, "weight": weight})

    for raw_type in schema_org_types or []:
        t = (raw_type or "").lower().strip()
        if t in _SCHEMA_ORG_TO_CATEGORY:
            cat, weight = _SCHEMA_ORG_TO_CATEGORY[t]
            _vote(cat, weight, "schema_org", t)

    seen_paths: set[str] = set()
    for route in observed_routes or []:
        if not route:
            continue
        parsed = urlparse(route if str(route).startswith("http") else "http://x" + str(route))
        path = (parsed.path or "").lower().rstrip("/")
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        for hint, category, weight in _PATH_HINTS:
            if hint in path:
                _vote(category, weight, "url_path", path)
                break

    if business_signals:
        for hint in business_signals.get("billing_model_hints") or []:
            for cat, weight in _BILLING_HINT_TO_CATEGORY.get(hint, []):
                _vote(cat, weight, "billing_model", hint)

    site_data = site_data or {}
    target_url = site_data.get("url") or ""
    if target_url:
        host = (urlparse(target_url).netloc or "").lower()
        for tld_pattern, cat, weight in _TLD_TO_CATEGORY:
            if host.endswith(tld_pattern):
                _vote(cat, weight, "domain_tld", host)
                break

    tech_corpus_pieces: list[str] = []
    tech_stack = site_data.get("tech_stack") or {}
    if isinstance(tech_stack, dict):
        for k, v in tech_stack.items():
            tech_corpus_pieces.append(str(k).lower())
            if isinstance(v, (list, tuple)):
                tech_corpus_pieces.extend(str(x).lower() for x in v)
            elif isinstance(v, str):
                tech_corpus_pieces.append(v.lower())
    cms = site_data.get("cms")
    if isinstance(cms, dict):
        tech_corpus_pieces.extend(str(k).lower() for k in cms.keys())
    if isinstance(cms, list):
        tech_corpus_pieces.extend(str(x).lower() for x in cms)

    if network_data:
        cms_net = network_data.get("cms_detected") or {}
        if isinstance(cms_net, dict):
            tech_corpus_pieces.extend(str(k).lower() for k in cms_net.keys())
        cdn_net = network_data.get("cdn_detected") or {}
        if isinstance(cdn_net, dict):
            tech_corpus_pieces.extend(str(k).lower() for k in cdn_net.keys())

    tech_corpus = " ".join(tech_corpus_pieces)
    for tech_name, hits in _TECH_TO_CATEGORY.items():
        if tech_name in tech_corpus:
            for cat, weight in hits:
                _vote(cat, weight, "tech_signal", tech_name)

    has_api_docs = bool(api_doc_data and (api_doc_data.get("openapi_specs") or api_doc_data.get("graphql_schemas")))
    if has_api_docs and scores.get("AI Platform", 0) >= 3:
        _vote("AI Platform", 2, "api_docs", "openapi_or_graphql_present_with_ai_signal")

    if business_signals and business_signals.get("tiers") and not any(scores.values()):
        _vote("CRM", 1, "fallback", "pricing_tiers_present_no_other_signal")

    if not any(scores.values()):
        fallback = _evidence_free_fallback(site_data)
        _vote(fallback, 1, "fallback", "no_signal_default")

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_category, top_score = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 and ranked[1][1] > 0 else None
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0

    if top_score >= 8 and top_score - runner_up_score >= 4:
        confidence = "high"
    elif top_score >= 4 and top_score - runner_up_score >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "category": top_category,
        "confidence": confidence,
        "scores": {k: v for k, v in scores.items() if v > 0},
        "evidence": evidence,
        "runner_up": runner_up,
    }


def _evidence_free_fallback(site_data: dict) -> str:
    description = (site_data.get("description") or "").lower()
    title = (site_data.get("title") or "").lower()
    blob = description + " " + title
    if any(k in blob for k in ("blog", "writer", "newsletter", "essay")):
        return "Publishing"
    if any(k in blob for k in ("forum", "discuss", "community")):
        return "Community"
    if any(k in blob for k in ("news", "today", "breaking", "headline")):
        return "News"
    return "Publishing"


def compute_scrape_coverage(
    site_data: dict | None,
    auth_data: dict | None,
    form_data: dict | None,
    observed_routes_count: int,
    vegeta_category: str | None = None,
) -> str:
    """Derive the scrape_coverage signal for the PRD bundle.

    Returns:
        One of: "marketing_only", "public_app_surface", "authenticated_captured".
    """
    try:
        from config import VEGETA_CATEGORY_EMPHASIS
    except ImportError:
        VEGETA_CATEGORY_EMPHASIS = {}

    site_data = site_data or {}
    auth_data = auth_data or {}
    form_data = form_data or {}

    has_auth = bool(auth_data.get("has_auth"))
    login_form = form_data.get("login_form") or auth_data.get("login_forms") or []
    has_login_route = bool(form_data.get("purpose_routes", {}).get("login"))
    has_dashboard_route = any(
        "/dashboard" in (r or "").lower() or "/app" in (r or "").lower()
        for r in (site_data.get("pages") or [])
    )

    is_gated = bool((VEGETA_CATEGORY_EMPHASIS or {}).get(vegeta_category, {}).get("gated"))

    if is_gated and not (has_dashboard_route and not has_login_route):
        return "marketing_only"

    if not has_auth and not has_login_route and not login_form:
        return "public_app_surface"

    if has_auth and not has_dashboard_route:
        return "marketing_only"

    if has_dashboard_route and not has_login_route:
        return "authenticated_captured"

    return "public_app_surface"
