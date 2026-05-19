"""Fetch contextually relevant stock images. Priority: Pexels → Pixabay → Unsplash."""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

PEXELS_BASE = "https://api.pexels.com/v1"
PIXABAY_BASE = "https://pixabay.com/api"
UNSPLASH_BASE = "https://api.unsplash.com"
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _make_request(url: str, headers: dict | None = None) -> urllib.request.Request:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return req


def _validate_image(path: str, aspect_range: tuple[float, float] = (1.2, 3.0)) -> bool:
    """Check downloaded image meets minimum quality requirements."""
    try:
        from PIL import Image
        img = Image.open(path)
        img.verify()
        img = Image.open(path)
        w, h = img.size
        if w < 800 or h < 500:
            logger.debug("Image rejected: %s (%dx%d below 800x500 minimum)", path, w, h)
            return False
        ratio = w / h
        min_r, max_r = aspect_range
        if ratio < min_r or ratio > max_r:
            logger.debug("Image rejected: %s (aspect ratio %.2f outside %.1f-%.1f range)", path, ratio, min_r, max_r)
            return False
        return True
    except Exception as e:
        logger.debug("Image rejected: %s (integrity check failed: %s)", path, e)
        return False


def _download_file(url: str, dest: str, aspect_range: tuple[float, float] = (1.2, 3.0)) -> bool:
    """Download a file with proper User-Agent header and validate dimensions/integrity."""
    try:
        req = _make_request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            with open(dest, "wb") as f:
                f.write(resp.read())
        if os.path.getsize(dest) < 1000:
            os.remove(dest)
            return False
        if not _validate_image(dest, aspect_range):
            os.remove(dest)
            return False
        return True
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)
        return False

_CATEGORY_FALLBACK_QUERIES = {
    "Cool Transition": ["creative agency team", "modern workspace"],
    "3D & WebGL / Game": ["technology innovation", "digital interface"],
    "SVG & Vector Graphics": ["graphic design creative", "digital artwork"],
    "Representation Format": ["editorial storytelling", "documentary photography"],
    "Normal Website": ["professional business", "corporate office"],
}

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "of", "and", "or", "in", "on",
    "at", "to", "for", "with", "by", "from", "this", "that", "it",
    "we", "our", "your", "all", "one", "new", "best", "more", "get",
    "can", "will", "just", "has", "have", "been", "into", "most",
    "dedicated", "committed", "leading", "innovative", "solutions",
    "world", "help", "make", "every", "about", "company",
})

_DOMAIN_KEYWORDS = {
    "agency": "digital marketing agency team",
    "marketing": "digital marketing strategy",
    "brand": "brand identity design",
    "studio": "creative studio workspace",
    "design": "design studio interior",
    "portfolio": "professional creative work",
    "shop": "ecommerce product lifestyle",
    "store": "retail shopping lifestyle",
    "commerce": "ecommerce product lifestyle",
    "blog": "editorial content journalism",
    "magazine": "editorial magazine print",
    "finance": "fintech dashboard technology",
    "banking": "finance corporate professional",
    "health": "healthcare wellness medical",
    "medical": "healthcare medical technology",
    "food": "restaurant culinary dining",
    "restaurant": "restaurant culinary dining",
    "travel": "travel destination aerial",
    "hotel": "luxury hotel hospitality",
    "fashion": "fashion editorial styling",
    "beauty": "beauty cosmetics lifestyle",
    "architecture": "architecture building interior",
    "real estate": "luxury property architecture",
    "property": "luxury real estate interior",
    "music": "music concert production",
    "education": "education university campus",
    "saas": "software technology startup",
    "platform": "technology platform digital",
    "crypto": "cryptocurrency blockchain technology",
    "nft": "digital art technology",
    "automotive": "automotive luxury vehicle",
    "sport": "sports athlete action",
    "fitness": "fitness gym workout",
    "photography": "photography camera creative",
    "film": "cinema film production",
    "gaming": "gaming esports technology",
    "sustainability": "sustainability nature environment",
    "energy": "renewable energy technology",
    "consulting": "business consulting corporate",
    "law": "corporate legal professional",
    "growth": "business growth startup team",
    "accelerator": "startup accelerator team",
    "venture": "venture capital startup",
}


def is_available() -> bool:
    return bool(
        os.environ.get("PEXELS_API_KEY")
        or os.environ.get("PIXABAY_API_KEY")
        or os.environ.get("UNSPLASH_ACCESS_KEY")
    )


def generate_queries(site_data: dict, content_map: dict | None = None) -> list[str]:
    """Derive search queries from site content — URL-specific, not just category."""
    queries = []
    description = site_data.get("description", "") or ""
    title = site_data.get("title", "") or ""
    category = site_data.get("category", "")
    url = site_data.get("url", "")

    combined_text = f"{description} {title} {url}".lower()

    # 1. Primary: domain/industry detection — most reliable for relevant images
    # Prefer matches in title (stronger signal) over description
    title_lower = title.lower()
    matched_domain = None
    title_match = None
    desc_match = None
    for domain_key, domain_query in _DOMAIN_KEYWORDS.items():
        if domain_key in title_lower and not title_match:
            title_match = (domain_key, domain_query)
        elif domain_key in combined_text and not desc_match:
            desc_match = (domain_key, domain_query)
    best = title_match or desc_match
    if best:
        matched_domain, domain_query = best
        queries.append(domain_query)

    # 2. Extract 2-3 meaningful nouns from description for a contextual query
    # Filter out brand name (from URL domain) to avoid irrelevant queries
    brand_words = set()
    if url:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.replace("www.", "").split(".")[0].lower()
        brand_words = {domain} | {w for w in domain.split("-") if len(w) > 3}

    if description:
        desc_words = [
            w for w in description.lower().replace(",", " ").replace(".", " ")
            .replace("|", " ").replace("-", " ").replace("'", " ").split()
            if w not in _STOP_WORDS and len(w) > 4 and not w.startswith("http")
            and w != matched_domain and w not in brand_words
        ]
        if len(desc_words) >= 2:
            queries.append(" ".join(desc_words[:2]) + " professional")

    # 3. Fallback: category-based if we have fewer than 2 queries
    if len(queries) < 2:
        fallback = _CATEGORY_FALLBACK_QUERIES.get(category, ["modern website professional"])
        for fq in fallback:
            if fq not in queries:
                queries.append(fq)
                if len(queries) >= 3:
                    break

    # 4. Final fallback: title keywords if still short
    if len(queries) < 2:
        title_words = [
            w for w in title.lower().replace("|", " ").replace("-", " ")
            .replace("—", " ").replace("®", "").replace("™", "").split()
            if w not in _STOP_WORDS and len(w) > 4
        ]
        if title_words:
            queries.append(" ".join(sorted(title_words, key=len, reverse=True)[:2]))

    return queries[:3]


def get_target_aspect_ratio(responsive_data: dict | None = None) -> tuple[float, float]:
    """Determine target aspect ratio range from site layout analysis.

    Returns (min_ratio, max_ratio) for image validation.
    """
    if not responsive_data:
        return (1.2, 3.0)

    breakpoints = responsive_data.get("breakpoints", {})
    if isinstance(breakpoints, dict):
        desktop = breakpoints.get("desktop_large") or breakpoints.get("desktop", {})
        if isinstance(desktop, dict):
            width = desktop.get("width", 1920)
            # Check hero section height if available
            sections = desktop.get("sections", [])
            if sections and isinstance(sections[0], dict):
                hero_h = sections[0].get("height", 0)
                if hero_h > 0 and width > 0:
                    hero_ratio = width / hero_h
                    # Widen range around the hero's aspect ratio
                    return (max(1.0, hero_ratio * 0.6), min(4.0, hero_ratio * 1.5))

    return (1.2, 3.0)


# ---------------------------------------------------------------------------
# Pexels (primary) — 200 req/hr, attribution encouraged but not required
# ---------------------------------------------------------------------------
def _fetch_pexels(queries: list[str], output_dir: str, count: int, credits: list, aspect_range: tuple[float, float] = (1.2, 3.0)) -> list[str]:
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        return []

    downloaded: list[str] = []
    for query in queries:
        if len(downloaded) >= count:
            break
        try:
            url = f"{PEXELS_BASE}/search?query={urllib.parse.quote(query)}&per_page=3&orientation=landscape"
            req = _make_request(url, {"Authorization": api_key})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())

            for photo in data.get("photos", []):
                if len(downloaded) >= count:
                    break
                img_url = photo.get("src", {}).get("large2x") or photo.get("src", {}).get("original")
                if not img_url:
                    continue
                filename = f"stock_pexels_{photo['id']}.jpg"
                dest = os.path.join(output_dir, filename)
                if not _download_file(img_url, dest, aspect_range):
                    continue
                downloaded.append(dest)
                credits.append({
                    "file": filename,
                    "source": "Pexels",
                    "photographer": photo.get("photographer", "Unknown"),
                    "photographer_url": photo.get("photographer_url", ""),
                    "photo_url": photo.get("url", ""),
                    "license": "Pexels License (free for commercial use)",
                })
                logger.info("Downloaded Pexels image: %s (query: %s)", filename, query)
        except Exception as e:
            logger.warning("Pexels API error for query '%s': %s", query, e)
            continue

    return downloaded


# ---------------------------------------------------------------------------
# Pixabay (secondary) — 100 req/min, NO attribution required
# ---------------------------------------------------------------------------
def _fetch_pixabay(queries: list[str], output_dir: str, count: int, credits: list, aspect_range: tuple[float, float] = (1.2, 3.0)) -> list[str]:
    api_key = os.environ.get("PIXABAY_API_KEY")
    if not api_key:
        return []

    downloaded: list[str] = []
    for query in queries:
        if len(downloaded) >= count:
            break
        try:
            url = (
                f"{PIXABAY_BASE}/?key={api_key}"
                f"&q={urllib.parse.quote(query)}&image_type=photo"
                f"&orientation=horizontal&per_page=3&safesearch=true"
            )
            req = _make_request(url)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())

            for photo in data.get("hits", []):
                if len(downloaded) >= count:
                    break
                img_url = photo.get("largeImageURL")
                if not img_url:
                    continue
                photo_id = photo.get("id", "unknown")
                filename = f"stock_pixabay_{photo_id}.jpg"
                dest = os.path.join(output_dir, filename)
                if not _download_file(img_url, dest, aspect_range):
                    continue
                downloaded.append(dest)
                credits.append({
                    "file": filename,
                    "source": "Pixabay",
                    "photographer": photo.get("user", "Unknown"),
                    "photographer_url": f"https://pixabay.com/users/{photo.get('user', '')}-{photo.get('user_id', '')}",
                    "photo_url": photo.get("pageURL", ""),
                    "license": "Pixabay Content License (free, no attribution required)",
                })
                logger.info("Downloaded Pixabay image: %s (query: %s)", filename, query)
        except Exception as e:
            logger.warning("Pixabay API error for query '%s': %s", query, e)
            continue

    return downloaded


# ---------------------------------------------------------------------------
# Unsplash (last resort) — 50 req/hr, attribution required
# ---------------------------------------------------------------------------
def _fetch_unsplash(queries: list[str], output_dir: str, count: int, credits: list, aspect_range: tuple[float, float] = (1.2, 3.0)) -> list[str]:
    api_key = os.environ.get("UNSPLASH_ACCESS_KEY")
    if not api_key:
        return []

    downloaded: list[str] = []
    for query in queries:
        if len(downloaded) >= count:
            break
        try:
            url = (
                f"{UNSPLASH_BASE}/search/photos"
                f"?query={urllib.parse.quote(query)}&per_page=3&orientation=landscape"
            )
            req = _make_request(url, {
                "Authorization": f"Client-ID {api_key}",
                "Accept-Version": "v1",
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())

            for photo in data.get("results", []):
                if len(downloaded) >= count:
                    break
                img_url = photo.get("urls", {}).get("regular")
                if not img_url:
                    continue

                # Trigger download event (mandatory for Unsplash compliance)
                dl_url = photo.get("links", {}).get("download_location")
                if dl_url:
                    try:
                        dl_req = _make_request(dl_url, {"Authorization": f"Client-ID {api_key}"})
                        urllib.request.urlopen(dl_req, timeout=5)
                    except Exception:
                        pass

                photo_id = photo.get("id", "unknown")
                filename = f"stock_unsplash_{photo_id}.jpg"
                dest = os.path.join(output_dir, filename)
                if not _download_file(img_url, dest, aspect_range):
                    continue
                downloaded.append(dest)
                user = photo.get("user", {})
                credits.append({
                    "file": filename,
                    "source": "Unsplash",
                    "photographer": user.get("name", "Unknown"),
                    "photographer_url": f"{user.get('links', {}).get('html', '')}?utm_source=leviathon&utm_medium=referral",
                    "photo_url": photo.get("links", {}).get("html", ""),
                    "license": "Unsplash License (free, attribution required)",
                })
                logger.info("Downloaded Unsplash image: %s (query: %s)", filename, query)
        except Exception as e:
            logger.warning("Unsplash API error for query '%s': %s", query, e)
            continue

    return downloaded


# ---------------------------------------------------------------------------
# Public API — called by the pipeline
# ---------------------------------------------------------------------------
def fetch_and_download(queries: list[str], output_dir: str, count: int = 2,
                       aspect_range: tuple[float, float] = (1.2, 3.0)) -> list[str]:
    """Fetch stock images: Pexels → Pixabay → Unsplash. Returns local file paths."""
    credits: list[dict] = []
    downloaded: list[str] = []

    # Priority 1: Pexels
    downloaded.extend(_fetch_pexels(queries, output_dir, count, credits, aspect_range))

    # Priority 2: Pixabay (fill remaining)
    if len(downloaded) < count:
        remaining = count - len(downloaded)
        downloaded.extend(_fetch_pixabay(queries, output_dir, remaining, credits, aspect_range))

    # Priority 3: Unsplash (last resort)
    if len(downloaded) < count:
        remaining = count - len(downloaded)
        downloaded.extend(_fetch_unsplash(queries, output_dir, remaining, credits, aspect_range))

    # Write credits file for compliance
    if credits:
        credits_path = os.path.join(output_dir, "image_credits.json")
        with open(credits_path, "w") as f:
            json.dump({
                "attribution": "Photos provided by Pexels (pexels.com), Pixabay (pixabay.com), and Unsplash (unsplash.com)",
                "images": credits,
            }, f, indent=2)
        logger.info("Wrote image credits to %s", credits_path)

    return downloaded
