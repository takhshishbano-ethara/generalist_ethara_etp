"""
Phase 2 – Style Extractor

Receives a Playwright page object (already navigated to the target site) and
extracts all visual design tokens by injecting inject_style_extractor.js, then
post-processes the raw data into a structured, JSON-serializable dictionary.

Returned dict keys:
    colors, fonts, type_scale, grid, spacing, css_variables, css_variables_by_scope,
    font_faces, gradients, shadows, border_radii, effects, seo, media_queries
"""

from __future__ import annotations

import colorsys
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from playwright.async_api import Page

# ---------------------------------------------------------------------------
# Path to the JS injection script
# ---------------------------------------------------------------------------
_JS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inject_style_extractor.js"


# ===================================================================
#  Colour helpers
# ===================================================================

def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """'#AABBCC' -> (170, 187, 204)"""
    h = hex_str.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _perceived_lightness(hex_str: str) -> float:
    """Return relative luminance (0 = black, 1 = white)."""
    r, g, b = (_c / 255.0 for _c in _hex_to_rgb(hex_str))
    # sRGB linearisation
    r = r / 12.92 if r <= 0.04045 else ((r + 0.055) / 1.055) ** 2.4
    g = g / 12.92 if g <= 0.04045 else ((g + 0.055) / 1.055) ** 2.4
    b = b / 12.92 if b <= 0.04045 else ((b + 0.055) / 1.055) ** 2.4
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex_to_hsl(hex_str: str) -> tuple[float, float, float]:
    r, g, b = (_c / 255.0 for _c in _hex_to_rgb(hex_str))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s, l  # hue in degrees, saturation, lightness


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb_to_lab(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Convert sRGB (0-255) to CIE Lab via XYZ D65."""
    lr = _srgb_to_linear(r / 255.0)
    lg = _srgb_to_linear(g / 255.0)
    lb = _srgb_to_linear(b / 255.0)

    x = (0.4124564 * lr + 0.3575761 * lg + 0.1804375 * lb) / 0.95047
    y = (0.2126729 * lr + 0.7151522 * lg + 0.0721750 * lb) / 1.00000
    z = (0.0193339 * lr + 0.1191920 * lg + 0.9503041 * lb) / 1.08883

    epsilon = 216.0 / 24389.0
    kappa = 24389.0 / 27.0

    def f(t):
        return t ** (1 / 3) if t > epsilon else (kappa * t + 16) / 116

    fx, fy, fz = f(x), f(y), f(z)
    L = 116 * fy - 16
    a = 500 * (fx - fy)
    b_val = 200 * (fy - fz)
    return L, a, b_val


def _color_distance(hex_a: str, hex_b: str) -> float:
    """CIE76 Delta-E perceptual color distance in Lab space."""
    L1, a1, b1 = _rgb_to_lab(*_hex_to_rgb(hex_a))
    L2, a2, b2 = _rgb_to_lab(*_hex_to_rgb(hex_b))
    return math.sqrt((L1 - L2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2)


_DELTA_E_DEDUP_THRESHOLD = 5.0


def _dedup_colors(colors: list[dict]) -> list[dict]:
    """Merge perceptually similar colors, keeping the higher-count entry."""
    if not colors:
        return colors
    result = []
    for c in colors:
        merged = False
        for existing in result:
            try:
                if _color_distance(c["hex"], existing["hex"]) < _DELTA_E_DEDUP_THRESHOLD:
                    existing["count"] += c.get("count", 0)
                    for u in c.get("usages", []):
                        if u not in existing.get("usages", []):
                            existing.setdefault("usages", []).append(u)
                    merged = True
                    break
            except (ValueError, KeyError):
                continue
        if not merged:
            result.append(c)
    return result


_BG_PROPS = {"backgroundColor"}
_TEXT_PROPS = {"color"}
_BORDER_PROPS = {
    "borderColor", "borderTopColor", "borderRightColor",
    "borderBottomColor", "borderLeftColor", "outlineColor",
}
_ACCENT_PROPS = {"color", "backgroundColor"}  # used for links/buttons – detected by heuristics
_SHADOW_PROPS = {"boxShadow"}


def _is_near_white(hex_str: str, threshold: float = 0.85) -> bool:
    return _perceived_lightness(hex_str) >= threshold


def _is_near_black(hex_str: str, threshold: float = 0.05) -> bool:
    return _perceived_lightness(hex_str) <= threshold


def _is_chromatic(hex_str: str) -> bool:
    _, s, _ = _hex_to_hsl(hex_str)
    return s > 0.15


def _assign_semantic_colors(raw_colors: list[dict]) -> list[dict]:
    """
    Walk the sorted-by-frequency colour list and assign semantic names.
    Strategy:
        - Most frequent background colour -> "Primary Background"
        - Most frequent text colour        -> "Primary Text"
        - Chromatic colours used on links/buttons -> "Accent" / "Secondary Accent"
        - Remaining prominent colours -> "Surface", "Border", "Shadow", etc.
    """
    assigned: list[dict] = []
    used_roles: set[str] = set()

    # Pre-categorise by usage property sets
    bg_colors = [c for c in raw_colors if _BG_PROPS & set(c.get("usages", []))]
    text_colors = [c for c in raw_colors if _TEXT_PROPS & set(c.get("usages", []))]
    border_colors = [c for c in raw_colors if _BORDER_PROPS & set(c.get("usages", []))]
    shadow_colors = [c for c in raw_colors if _SHADOW_PROPS & set(c.get("usages", []))]

    role_map: dict[str, str | None] = {}  # hex -> role

    # --- Primary Background ---
    for c in bg_colors:
        if "Primary Background" not in used_roles:
            role_map[c["hex"]] = "Primary Background"
            used_roles.add("Primary Background")
            break

    # --- Secondary Background / Surface ---
    for c in bg_colors:
        if c["hex"] not in role_map:
            role_map[c["hex"]] = "Secondary Background"
            used_roles.add("Secondary Background")
            break

    # --- Primary Text ---
    for c in text_colors:
        if c["hex"] not in role_map:
            role_map[c["hex"]] = "Primary Text"
            used_roles.add("Primary Text")
            break

    # --- Secondary Text ---
    for c in text_colors:
        if c["hex"] not in role_map:
            role_map[c["hex"]] = "Secondary Text"
            used_roles.add("Secondary Text")
            break

    # --- Accent (chromatic, used on text or background) ---
    accent_idx = 0
    accent_labels = ["Accent", "Secondary Accent", "Tertiary Accent"]
    for c in raw_colors:
        if accent_idx >= len(accent_labels):
            break
        if c["hex"] in role_map:
            continue
        if _is_chromatic(c["hex"]) and not _is_near_white(c["hex"]) and not _is_near_black(c["hex"]):
            role_map[c["hex"]] = accent_labels[accent_idx]
            used_roles.add(accent_labels[accent_idx])
            accent_idx += 1

    # --- Border ---
    for c in border_colors:
        if c["hex"] not in role_map:
            role_map[c["hex"]] = "Border"
            used_roles.add("Border")
            break

    # --- Shadow ---
    for c in shadow_colors:
        if c["hex"] not in role_map:
            role_map[c["hex"]] = "Shadow"
            used_roles.add("Shadow")
            break

    # Build output list (preserve frequency order)
    for c in raw_colors:
        entry = {
            "hex": c["hex"],
            "count": c["count"],
            "usages": c.get("usages", []),
            "role": role_map.get(c["hex"]),
        }
        assigned.append(entry)

    return assigned


# ===================================================================
#  Font helpers
# ===================================================================

def _build_font_inventory(raw_fonts: list[dict], raw_font_faces: list[dict],
                          loaded_fonts: list[dict]) -> list[dict]:
    """
    Merge raw font list with @font-face declarations and document.fonts API
    data to produce a clean inventory.
    """
    inventory: dict[str, dict] = {}

    for f in raw_fonts:
        family = f["family"]
        if family not in inventory:
            inventory[family] = {
                "family": family,
                "weights": sorted(set(f.get("weights", []))),
                "count": f.get("count", 0),
                "source": "computed",
                "styles": set(),
            }
        else:
            inventory[family]["weights"] = sorted(
                set(inventory[family]["weights"]) | set(f.get("weights", []))
            )
            inventory[family]["count"] += f.get("count", 0)

    # Enrich with @font-face data
    for ff in raw_font_faces:
        family = ff.get("family", "").strip()
        if not family:
            continue
        if family not in inventory:
            inventory[family] = {
                "family": family,
                "weights": [],
                "count": 0,
                "source": "@font-face",
                "styles": set(),
            }
        w = ff.get("weight", "normal")
        inventory[family]["weights"] = sorted(set(inventory[family]["weights"]) | {w})
        inventory[family]["styles"].add(ff.get("style", "normal"))
        if ff.get("src"):
            inventory[family].setdefault("src_sample", ff["src"])

    # Enrich with document.fonts
    for lf in loaded_fonts:
        family = lf.get("family", "").strip()
        if not family:
            continue
        if family not in inventory:
            inventory[family] = {
                "family": family,
                "weights": [],
                "count": 0,
                "source": "document.fonts",
                "styles": set(),
            }
        inventory[family]["weights"] = sorted(
            set(inventory[family]["weights"]) | {lf.get("weight", "normal")}
        )
        inventory[family]["styles"].add(lf.get("style", "normal"))

    # Convert sets to lists for JSON serialisation
    results = []
    for info in inventory.values():
        info["styles"] = sorted(info["styles"]) if info.get("styles") else ["normal"]
        results.append(info)

    # Sort by usage count descending
    results.sort(key=lambda x: x["count"], reverse=True)
    return results


# ===================================================================
#  Type scale helpers
# ===================================================================

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# Rough expected pixel ranges for clustering (desktop)
_ROLE_RANGES = [
    ("Display",  48, 200),
    ("H1",       34, 60),
    ("H2",       26, 40),
    ("H3",       20, 30),
    ("H4",       18, 24),
    ("Body",     14, 18),
    ("Small",    11, 14),
    ("Caption",   8, 12),
]


def _cluster_type_scale(raw_scale: list[dict]) -> list[dict]:
    """
    Assign a semantic role (H1, H2, Body, etc.) to each entry.
    Uses a combination of tag heuristics and size ranges.
    """
    if not raw_scale:
        return []

    # Sort largest-first (already sorted from JS but be safe)
    entries = sorted(raw_scale, key=lambda x: x["size_px"], reverse=True)

    # First pass: assign by heading tag if present
    tag_assigned: dict[str, bool] = {}
    for entry in entries:
        tags = set(entry.get("tags", []))
        heading_tags = tags & _HEADING_TAGS
        if heading_tags:
            # Pick the most significant heading tag present
            best = sorted(heading_tags, key=lambda t: int(t[1]))[0]
            label = best.upper()
            if label not in tag_assigned:
                entry["role"] = label
                tag_assigned[label] = True

    # Second pass: assign remaining by size range
    for entry in entries:
        if "role" in entry:
            continue
        size = entry["size_px"]
        tags = set(entry.get("tags", []))

        # Check for body-level text
        if "p" in tags and 13 <= size <= 20:
            if "Body" not in tag_assigned:
                entry["role"] = "Body"
                tag_assigned["Body"] = True
                continue
            elif "Body Large" not in tag_assigned and size > 16:
                entry["role"] = "Body Large"
                tag_assigned["Body Large"] = True
                continue

        # Check for small / caption text
        if size <= 13:
            if "Caption" not in tag_assigned:
                entry["role"] = "Caption"
                tag_assigned["Caption"] = True
                continue
            else:
                entry["role"] = "Small"
                continue

        # Fallback: match by size range
        for role_name, lo, hi in _ROLE_RANGES:
            if lo <= size <= hi and role_name not in tag_assigned:
                entry["role"] = role_name
                tag_assigned[role_name] = True
                break

    # Final pass: anything still unassigned gets a generic label
    for entry in entries:
        if "role" not in entry:
            entry["role"] = f"{entry['size_px']}px"

    # Clean up line_height to string
    for entry in entries:
        if isinstance(entry.get("line_height"), (int, float)) and entry["line_height"] > 0:
            entry["line_height_px"] = entry["line_height"]
        entry.pop("count", None)

    return entries


# ===================================================================
#  Grid helpers
# ===================================================================

def _process_grid(raw_grid: dict) -> dict:
    """Detect column count, max-width, and gutters from raw grid data."""
    result: dict[str, Any] = {}

    # Max widths
    max_widths = raw_grid.get("max_widths", [])
    if max_widths:
        result["max_width"] = max_widths[0]
        result["all_max_widths"] = max_widths

    # Parse grid layouts for column counts and gaps
    layouts = raw_grid.get("layouts", [])
    column_counts: list[int] = []
    gutters: list[str] = []

    for layout in layouts:
        cols_str = layout.get("columns", "")
        gap_str = layout.get("gap", "")

        # Count columns from gridTemplateColumns
        if cols_str and cols_str != "none":
            # Count space-separated values (each is a column track)
            # Handle repeat() notation
            repeat_match = re.search(r"repeat\((\d+)", cols_str)
            if repeat_match:
                column_counts.append(int(repeat_match.group(1)))
            else:
                # Count individual track values
                tracks = [t.strip() for t in cols_str.split() if t.strip()]
                if tracks:
                    column_counts.append(len(tracks))

        if gap_str and gap_str != "normal" and gap_str != "0px":
            gutters.append(gap_str)

    if column_counts:
        # Most common column count
        freq: dict[int, int] = defaultdict(int)
        for c in column_counts:
            freq[c] += 1
        result["detected_columns"] = max(freq, key=freq.get)  # type: ignore[arg-type]
        result["all_column_counts"] = sorted(set(column_counts))

    if gutters:
        result["gutters"] = sorted(set(gutters))

    result["layouts"] = layouts

    return result


# ===================================================================
#  Spacing helpers
# ===================================================================

def _process_spacing(raw_spacing: dict) -> dict:
    """Detect baseline grid unit and organise spacing scale."""
    result: dict[str, Any] = {}
    values = raw_spacing.get("values", [])
    baseline = raw_spacing.get("baseline_unit")

    result["values"] = values
    if baseline and baseline > 0:
        result["baseline_unit"] = baseline
        # Build a scale as multiples of the baseline
        scale: dict[str, int] = {}
        for v in values:
            multiple = round(v / baseline)
            if multiple > 0:
                scale[f"{multiple}x"] = v
        result["scale"] = scale
    else:
        # Attempt to find a common factor ourselves
        filtered = [v for v in values if 4 <= v <= 200]
        if len(filtered) >= 2:
            g = filtered[0]
            for v in filtered[1:10]:
                g = math.gcd(g, v)
            if g >= 2:
                result["baseline_unit"] = g

    return result


# ===================================================================
#  Google Fonts detection
# ===================================================================

_GOOGLE_FONTS_RE = re.compile(
    r"fonts\.googleapis\.com/css2?\?family=([^&\"'>\s]+)", re.IGNORECASE
)


async def _detect_google_fonts(page: Page) -> list[str]:
    """Extract Google Font family names from <link> tags in the page."""
    families: list[str] = []
    try:
        links = await page.eval_on_selector_all(
            'link[href*="fonts.googleapis.com"]',
            "els => els.map(e => e.href)"
        )
        for href in links:
            match = _GOOGLE_FONTS_RE.search(href)
            if match:
                raw = match.group(1)
                # "Roboto+Slab:wght@400;700|Open+Sans" -> ["Roboto Slab", "Open Sans"]
                for part in raw.split("|"):
                    name = part.split(":")[0].replace("+", " ")
                    if name:
                        families.append(name)
    except Exception:
        pass
    return sorted(set(families))


async def _detect_font_urls_from_network(page: Page) -> list[dict]:
    """
    Inspect any previously-captured network responses for font file URLs.
    This relies on the caller having set up response interception before
    navigation. If not, we fall back to scanning stylesheet text.
    """
    font_urls: list[dict] = []

    # Fallback: parse stylesheet text for url() references to font files
    try:
        urls = await page.evaluate("""
            (() => {
                const fontUrls = [];
                const fontExtRe = /url\\(["']?([^"')]+\\.(?:woff2?|ttf|otf|eot))["']?\\)/gi;
                for (const sheet of document.styleSheets) {
                    try {
                        for (const rule of sheet.cssRules) {
                            const text = rule.cssText || '';
                            let m;
                            fontExtRe.lastIndex = 0;
                            while ((m = fontExtRe.exec(text)) !== null) {
                                fontUrls.push(m[1]);
                            }
                        }
                    } catch (e) { /* cross-origin */ }
                }
                return [...new Set(fontUrls)];
            })()
        """)
        for url in urls:
            ext = url.rsplit(".", 1)[-1].lower().split("?")[0] if "." in url else "unknown"
            font_urls.append({"url": url, "format": ext})
    except Exception:
        pass

    return font_urls


# ===================================================================
#  Main entry point
# ===================================================================

async def extract_styles(page: Page) -> dict:
    """
    Inject the style-extractor JS into *page* (which must already be
    navigated to the target site) and return a processed dictionary of
    design tokens.

    Returns a JSON-serializable dict with keys:
        colors, fonts, type_scale, grid, spacing, css_variables,
        css_variables_by_scope, font_faces, gradients, shadows,
        border_radii, effects, seo, media_queries
    """
    # 1. Read and inject the extraction script
    js_code = _JS_PATH.read_text(encoding="utf-8")
    raw: dict = await page.evaluate(js_code)

    # 2. Post-process each section
    colors = _dedup_colors(raw.get("colors", []))
    colors = _assign_semantic_colors(colors)
    fonts = _build_font_inventory(
        raw.get("fonts", []),
        raw.get("font_faces", []),
        raw.get("loaded_fonts", []),
    )
    type_scale = _cluster_type_scale(raw.get("type_scale", []))
    grid = _process_grid(raw.get("grid", {}))
    spacing = _process_spacing(raw.get("spacing", {}))
    css_variables = raw.get("css_variables", {})

    # 3. Detect Google Fonts and font file URLs
    google_fonts = await _detect_google_fonts(page)
    font_file_urls = await _detect_font_urls_from_network(page)

    # Merge Google Font info into font inventory
    for gf in google_fonts:
        found = False
        for f in fonts:
            if f["family"].lower() == gf.lower():
                f["source"] = "Google Fonts"
                found = True
                break
        if not found:
            fonts.append({
                "family": gf,
                "weights": [],
                "count": 0,
                "source": "Google Fonts",
                "styles": ["normal"],
            })

    # Build font_faces section combining @font-face declarations + network URLs
    font_faces = raw.get("font_faces", [])
    if font_file_urls:
        # Append discovered font file URLs that aren't already represented
        existing_srcs = {ff.get("src", "") for ff in font_faces}
        for fu in font_file_urls:
            if not any(fu["url"] in s for s in existing_srcs if s):
                font_faces.append({
                    "family": _guess_family_from_url(fu["url"]),
                    "weight": "unknown",
                    "style": "normal",
                    "src": fu["url"],
                    "format": fu["format"],
                })

    return {
        "colors": colors,
        "fonts": fonts,
        "type_scale": type_scale,
        "grid": grid,
        "spacing": spacing,
        "css_variables": css_variables,
        "css_variables_by_scope": raw.get("css_variables_by_scope", {}),
        "font_faces": font_faces,
        "gradients": raw.get("gradients", []),
        "shadows": raw.get("shadows", []),
        "border_radii": raw.get("border_radii", []),
        "effects": raw.get("effects", {}),
        "seo": raw.get("seo", {}),
        "media_queries": raw.get("media_queries", []),
    }


def _guess_family_from_url(url: str) -> str:
    """
    Best-effort extraction of a font family name from a URL path.
    e.g. '/fonts/RobotoSlab-Bold.woff2' -> 'RobotoSlab'
    """
    # Grab the filename without extension
    filename = url.rsplit("/", 1)[-1].split("?")[0]
    name = filename.rsplit(".", 1)[0]
    # Strip common weight/style suffixes
    for suffix in [
        "-Regular", "-Bold", "-Italic", "-Light", "-Medium", "-SemiBold",
        "-ExtraBold", "-Black", "-Thin", "-ExtraLight",
        "_Regular", "_Bold", "_Italic", "_Light", "_Medium",
    ]:
        name = name.replace(suffix, "")
    # Insert spaces before capitals: "RobotoSlab" -> "Roboto Slab"
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return name.strip() or "Unknown"
