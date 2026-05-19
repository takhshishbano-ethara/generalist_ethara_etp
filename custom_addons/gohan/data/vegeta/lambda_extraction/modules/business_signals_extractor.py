"""
Business signals extractor.

Visits the target host's pricing/plans/billing pages and extracts:
    - Pricing tier names and headline prices
    - Billing model hints (per-seat, flat subscription, transaction fee,
      advertising, free tier, contact-only enterprise tier)
    - Currency observed
    - Per-tier feature bullets when they live next to the price

Output drives PRD Section 2 (Goals / business model), Section 3 (Roles, since
tier names usually map to roles), and the role tiering in Section 11
(Category-Specific Guidelines).

The extractor opens its own page on the existing browser context, navigates
and extracts, then closes that page. The caller's original page is untouched.
A failed pricing route is the expected case for many sites; the function
returns whatever it finds and never raises.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


_NAV_TIMEOUT_MS = 15_000
_SETTLE_MS = 1_500
_MAX_TIERS = 8
_MAX_FEATURES_PER_TIER = 12


_PRICE_RE = re.compile(
    r"(?P<symbol>[\$\u20AC\u00A3\u00A5\u20B9])\s?"
    r"(?P<amount>\d{1,3}(?:[,\.]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<period>/\s*(?:mo|month|monthly|yr|year|annually|user|seat|member|agent|hour|hr))?",
    re.IGNORECASE,
)

_CONTACT_RE = re.compile(r"\b(contact\s+(?:us|sales)|talk\s+to\s+sales|get\s+a\s+(?:quote|demo)|custom\s+pricing)\b", re.IGNORECASE)
_FREE_RE = re.compile(r"\b(free\s+(?:tier|plan|forever)|free\s+for\s+up\s+to|\$0(?:\.00)?\s*/?\s*(?:mo|month|user|seat)?|free\s+to\s+(?:start|use))\b", re.IGNORECASE)
_PER_SEAT_RE = re.compile(r"\b(per\s+(?:user|seat|member|agent|editor|host|developer)|/(?:user|seat|member|agent))\b", re.IGNORECASE)
_PER_USAGE_RE = re.compile(r"\b(per\s+(?:1k|1,000|million|m\s+)?\s*(?:token|request|call|message|event|run|execution|build|minute|gb|api\s+call)s?|pay\s+as\s+you\s+go|usage[- ]based)\b", re.IGNORECASE)
_TRANSACTION_FEE_RE = re.compile(r"\b(transaction\s+fee|service\s+fee|host\s+fee|guest\s+fee|booking\s+fee|listing\s+fee|\d{1,2}(?:\.\d{1,2})?\s?%\s+(?:per|of|transaction|booking|sale|payout|fee))\b", re.IGNORECASE)
_ADVERTISING_RE = re.compile(r"\b(ad[- ]supported|advertising[- ]supported|free\s+with\s+ads|sponsored\s+listings?)\b", re.IGNORECASE)
_GOV_FUNDED_RE = re.compile(r"\b(government[- ]funded|publicly\s+funded|free\s+for\s+(?:residents|citizens|students))\b", re.IGNORECASE)


_PRICING_DOM_EXTRACT_JS = r"""
() => {
    const tierSelectors = [
        '[class*="pricing-tier"]', '[class*="pricing-card"]', '[class*="pricing-plan"]',
        '[class*="plan-card"]', '[class*="plan-tier"]',
        '[class*="PricingTier"]', '[class*="PricingCard"]', '[class*="PricingPlan"]',
        '[class*="tier"]', '[class*="plan"]',
        '[data-testid*="pricing"]', '[data-testid*="plan"]',
        'article[class*="price"]', 'section[class*="price"]',
    ];
    const seen = new Set();
    const out = [];
    for (const sel of tierSelectors) {
        let nodes;
        try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
        for (const node of nodes) {
            if (seen.has(node)) continue;
            const rect = node.getBoundingClientRect();
            if (rect.width < 80 || rect.height < 80) continue;
            seen.add(node);
            const name = (
                node.querySelector('h1, h2, h3, h4, [class*="title"], [class*="name"]')?.textContent
                || ''
            ).trim().replace(/\s+/g, ' ').slice(0, 80);
            const priceText = (
                node.querySelector('[class*="price"], [class*="amount"], [class*="cost"]')?.textContent
                || node.textContent || ''
            ).replace(/\s+/g, ' ').slice(0, 200);
            const features = [];
            const featNodes = node.querySelectorAll('li, [class*="feature"], [class*="benefit"], [class*="perk"]');
            for (const f of featNodes) {
                const t = (f.textContent || '').replace(/\s+/g, ' ').trim();
                if (t && t.length > 2 && t.length < 200) features.push(t);
                if (features.length >= 20) break;
            }
            const ctaNode = node.querySelector('a[href], button');
            const cta = ctaNode ? (ctaNode.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60) : '';
            const ctaHref = ctaNode && ctaNode.tagName === 'A' ? ctaNode.getAttribute('href') : '';
            out.push({ name, priceText, features, cta, ctaHref });
            if (out.length >= 24) break;
        }
        if (out.length >= 24) break;
    }
    const bodyText = (document.body?.innerText || '').slice(0, 16000);
    return { tiers: out, bodyText };
}
"""


async def extract_business_signals(page, base_url: str, observed_routes: list[str]) -> dict[str, Any]:
    """Visit pricing/plans pages and extract tier and billing-model signals.

    Args:
        page: Playwright Page with a live BrowserContext. A scratch page is
            opened on its context; the caller's page is not navigated.
        base_url: The site origin URL (scheme + host). Path is ignored.
        observed_routes: Routes already discovered (URLs or path-only). Used to
            pick which canonical pricing route to visit first.

    Returns:
        {
            "found_pricing_url": str or None,
            "tiers": [{name, price, period, currency, contact_only:bool, features[], cta}, ...],
            "billing_model_hints": [str, ...],
            "free_tier_present": bool,
            "enterprise_contact_only": bool,
            "currency": str or None,
            "raw_text_sample": str,
            "probe_log": [{path, status, found_pricing:bool}, ...],
        }
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    try:
        from config import PRICING_ROUTES
    except ImportError:
        PRICING_ROUTES = ["/pricing", "/plans", "/billing"]

    candidates = _select_pricing_candidates(observed_routes, PRICING_ROUTES, origin)

    result: dict[str, Any] = {
        "found_pricing_url": None,
        "tiers": [],
        "billing_model_hints": [],
        "free_tier_present": False,
        "enterprise_contact_only": False,
        "currency": None,
        "raw_text_sample": "",
        "probe_log": [],
    }

    if not candidates:
        return result

    context = page.context
    scratch = None
    try:
        scratch = await context.new_page()
    except Exception as exc:
        logger.warning("business_signals: could not open scratch page: %s", exc)
        return result

    try:
        for candidate in candidates:
            log_entry = {"path": candidate, "status": None, "found_pricing": False}
            try:
                response = await scratch.goto(candidate, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                log_entry["status"] = response.status if response else None
                if not response or response.status >= 400:
                    result["probe_log"].append(log_entry)
                    continue
                await scratch.wait_for_timeout(_SETTLE_MS)

                raw = await scratch.evaluate(_PRICING_DOM_EXTRACT_JS)
                tiers, currency = _normalise_tiers(raw.get("tiers") or [])
                body_text = raw.get("bodyText") or ""

                hints = _detect_billing_hints(body_text)
                free_tier = bool(_FREE_RE.search(body_text)) or any(_tier_is_free(t) for t in tiers)
                enterprise_contact = any(t.get("contact_only") for t in tiers) or bool(_CONTACT_RE.search(body_text))

                if tiers or any([free_tier, enterprise_contact, hints]):
                    log_entry["found_pricing"] = True
                    result["found_pricing_url"] = candidate
                    result["tiers"] = tiers[:_MAX_TIERS]
                    result["billing_model_hints"] = hints
                    result["free_tier_present"] = free_tier
                    result["enterprise_contact_only"] = enterprise_contact
                    result["currency"] = currency
                    result["raw_text_sample"] = body_text[:1500]
                    result["probe_log"].append(log_entry)
                    break

                result["probe_log"].append(log_entry)
            except Exception as exc:
                log_entry["status"] = f"error: {type(exc).__name__}"
                result["probe_log"].append(log_entry)
                continue
    finally:
        try:
            await scratch.close()
        except Exception:
            pass

    return result


def _select_pricing_candidates(observed_routes: list[str], canonical: list[str], origin: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def _add(url: str) -> None:
        if url in seen:
            return
        seen.add(url)
        out.append(url)

    canonical_set = {p.rstrip("/").lower() for p in canonical}
    for route in observed_routes or []:
        if not route:
            continue
        full = route if route.startswith("http") else urljoin(origin + "/", route.lstrip("/"))
        parsed = urlparse(full)
        if parsed.netloc and parsed.netloc != urlparse(origin).netloc:
            continue
        path = parsed.path.rstrip("/").lower() or "/"
        if path in canonical_set or any(path.endswith(c) for c in canonical_set):
            _add(full)

    for path in canonical:
        _add(urljoin(origin + "/", path.lstrip("/")))

    return out


_CURRENCY_BY_SYMBOL = {
    "$": "USD",
    "\u20AC": "EUR",
    "\u00A3": "GBP",
    "\u00A5": "JPY",
    "\u20B9": "INR",
}


def _normalise_tiers(raw_tiers: list[dict]) -> tuple[list[dict], str | None]:
    out: list[dict] = []
    seen_names: set[str] = set()
    detected_currency: str | None = None

    for raw in raw_tiers:
        name = (raw.get("name") or "").strip()
        price_text = (raw.get("priceText") or "").strip()
        features_raw = raw.get("features") or []
        cta = (raw.get("cta") or "").strip()

        if not name and not price_text:
            continue

        match = _PRICE_RE.search(price_text)
        price: str | None = None
        period: str | None = None
        currency: str | None = None
        if match:
            symbol = match.group("symbol")
            amount = match.group("amount")
            period_raw = match.group("period") or ""
            price = f"{symbol}{amount}"
            period = period_raw.replace("/", "").strip().lower() or None
            currency = _CURRENCY_BY_SYMBOL.get(symbol)
            if currency and not detected_currency:
                detected_currency = currency

        contact_only = bool(_CONTACT_RE.search(price_text) or _CONTACT_RE.search(cta))

        features = []
        for f in features_raw[:_MAX_FEATURES_PER_TIER]:
            text = (f or "").strip()
            if text and text.lower() not in {x.lower() for x in features}:
                features.append(text)

        key = (name or price_text)[:60].lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        out.append({
            "name": name[:80] or None,
            "price": price,
            "period": period,
            "currency": currency,
            "contact_only": contact_only and not price,
            "features": features,
            "cta": cta or None,
        })

    return out, detected_currency


def _tier_is_free(tier: dict) -> bool:
    price = tier.get("price") or ""
    if not price:
        return False
    return bool(re.search(r"\$?0(?:\.00)?$", price)) or "free" in (tier.get("name") or "").lower()


def _detect_billing_hints(body_text: str) -> list[str]:
    hints: list[str] = []
    if _PER_SEAT_RE.search(body_text):
        hints.append("per-seat")
    if _PER_USAGE_RE.search(body_text):
        hints.append("usage-based")
    if _TRANSACTION_FEE_RE.search(body_text):
        hints.append("transaction-fee")
    if _ADVERTISING_RE.search(body_text):
        hints.append("advertising-supported")
    if _GOV_FUNDED_RE.search(body_text):
        hints.append("government-funded")
    if _FREE_RE.search(body_text):
        hints.append("free-tier")
    if _CONTACT_RE.search(body_text):
        hints.append("enterprise-contact-sales")
    return hints
