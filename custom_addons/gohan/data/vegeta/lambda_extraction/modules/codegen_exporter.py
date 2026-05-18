"""
Phase 8A: Code-Gen JSON Export.

Generates structured JSON files that an LLM can consume alongside the PRD
and wireframes to generate React code for a scraped website.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_JS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inject_component_tree.js"


async def export_codegen_files(
    page,
    output_dir: str,
    site_data: dict,
    style_data: dict,
    animation_data: dict,
    responsive_data: dict,
    asset_data: dict,
    network_data: dict | None = None,
    performance_data: dict | None = None,
    brand_data: dict | None = None,
    component_tokens: dict | None = None,
    dark_mode_data: dict | None = None,
) -> dict:
    """Generate all 6 code-gen JSON files. Returns summary stats."""

    js = _JS_PATH.read_text(encoding="utf-8")
    dom_data = await page.evaluate(js)

    tree = dom_data.get("tree", {})
    content_map = dom_data.get("contentMap", {})
    site_metadata_raw = dom_data.get("siteMetadata", {})

    stats = {}

    component_tree = tree or {}
    _save_json(component_tree, os.path.join(output_dir, "component_tree.json"))
    stats["component_count"] = _count_nodes(component_tree)

    _save_json(content_map, os.path.join(output_dir, "content_map.json"))
    stats["section_count"] = len(content_map)

    design_tokens = _build_design_tokens(style_data, responsive_data, component_tokens, dark_mode_data)
    _save_json(design_tokens, os.path.join(output_dir, "design_tokens.json"))
    stats["token_count"] = sum(len(v) if isinstance(v, (dict, list)) else 1 for v in design_tokens.values())

    asset_manifest = _build_asset_manifest(asset_data, content_map)
    _save_json(asset_manifest, os.path.join(output_dir, "asset_manifest.json"))
    stats["asset_count"] = sum(len(v) for v in asset_manifest.values() if isinstance(v, list))

    site_metadata = _build_site_metadata(site_metadata_raw, site_data, network_data, brand_data)
    _save_json(site_metadata, os.path.join(output_dir, "site_metadata.json"))

    animation_map = _build_animation_map(animation_data)
    _save_json(animation_map, os.path.join(output_dir, "animation_map.json"))
    stats["animation_element_count"] = len(animation_map.get("elements", {}))

    return stats


def _save_json(data: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def _count_nodes(node: dict) -> int:
    if not isinstance(node, dict) or node.get("_collapsed"):
        return 0
    count = 1
    for child in node.get("children", []):
        count += _count_nodes(child)
    return count


# ---------------------------------------------------------------------------
# Design Tokens
# ---------------------------------------------------------------------------
def _build_design_tokens(style_data: dict, responsive_data: dict,
                         component_tokens: dict | None = None,
                         dark_mode_data: dict | None = None) -> dict:
    tokens = {}

    colors = style_data.get("colors", [])
    semantic_roles = [
        "background", "text", "textSecondary", "accent", "surface",
        "border", "hover", "active", "muted", "highlight",
    ]
    color_tokens = {}
    for i, c in enumerate(colors[:10]):
        role = semantic_roles[i] if i < len(semantic_roles) else f"color{i+1}"
        color_tokens[role] = {"hex": c["hex"], "usage": c.get("count", 0)}
    tokens["colors"] = color_tokens

    fonts = style_data.get("fonts", [])
    font_roles = ["heading", "body", "accent", "mono", "icon"]
    font_tokens = {}
    for i, f in enumerate(fonts[:5]):
        role = font_roles[i] if i < len(font_roles) else f"font{i+1}"
        font_tokens[role] = {
            "family": f["family"],
            "weights": f.get("weights", []),
            "count": f.get("count", 0),
        }
    tokens["fonts"] = font_tokens

    type_scale = style_data.get("type_scale", [])
    level_names = [
        "displayXL", "h1", "h2", "h3", "subheading",
        "bodyLarge", "body", "caption", "label", "small",
    ]
    typography = {}
    for i, ts in enumerate(type_scale[:10]):
        name = level_names[i] if i < len(level_names) else f"level{i+1}"
        typography[name] = {
            "fontSize": f"{ts['size_px']}px",
            "lineHeight": ts.get("line_height", "normal"),
            "letterSpacing": ts.get("letter_spacing", "0"),
            "fontFamily": ts.get("font_family", "inherit"),
            "fontWeight": ts.get("font_weight", "400"),
        }
    tokens["typography"] = typography

    spacing = style_data.get("spacing", {})
    tokens["spacing"] = {
        "baselineUnit": spacing.get("baseline_unit", 8),
        "scale": spacing.get("values", [4, 8, 12, 16, 24, 32, 48, 64]),
    }

    grid = style_data.get("grid", {})
    tokens["grid"] = {
        "maxWidths": grid.get("all_max_widths", [grid.get("max_width", 1200)]),
        "layouts": grid.get("layouts", []),
    }

    breakpoints = responsive_data.get("breakpoints", {})
    bp_tokens = {}
    for name, bp in breakpoints.items():
        bp_tokens[name] = f"{bp.get('width', 0)}px"
    if not bp_tokens:
        bp_tokens = {"mobile": "375px", "tablet": "768px", "desktop": "1024px", "wide": "1440px"}
    tokens["breakpoints"] = bp_tokens

    effects = {}
    shadows = style_data.get("shadows", [])
    if shadows:
        effects["shadows"] = [{"value": s["value"], "count": s["count"]} for s in shadows[:6]]
    gradients = style_data.get("gradients", [])
    if gradients:
        effects["gradients"] = [{"value": g["value"][:200], "count": g["count"]} for g in gradients[:6]]
    radii = style_data.get("border_radii", [])
    if radii:
        effects["borderRadii"] = [{"value": r["value"], "count": r["count"]} for r in radii[:6]]
    visual_effects = style_data.get("effects", {})
    if visual_effects:
        for effect_type, entries in visual_effects.items():
            if isinstance(entries, list) and entries:
                effects[effect_type] = entries[:5]
    tokens["effects"] = effects

    css_vars = style_data.get("css_variables", {})
    if css_vars:
        tokens["cssVariables"] = dict(list(css_vars.items())[:30])

    seo = style_data.get("seo", {})
    if seo:
        tokens["seo"] = seo

    media_queries = style_data.get("media_queries", [])
    if media_queries:
        tokens["mediaQueries"] = media_queries

    if component_tokens:
        tokens["componentTokens"] = component_tokens

    if dark_mode_data and dark_mode_data.get("has_dark_mode"):
        tokens["darkMode"] = {
            "colors": dark_mode_data.get("dark_colors", {}),
            "cssVariableOverrides": dark_mode_data.get("css_variable_overrides", {}),
            "detectionMethod": dark_mode_data.get("detection_method"),
        }

    return tokens


# ---------------------------------------------------------------------------
# Asset Manifest
# ---------------------------------------------------------------------------
def _build_asset_manifest(asset_data: dict, content_map: dict) -> dict:
    manifest = {"images": [], "svgs": [], "fonts": [], "videos": [], "json": []}
    assets = asset_data.get("assets", {})

    section_media = {}
    for sec_key, sec in content_map.items():
        for m in sec.get("media", []):
            src = m.get("src")
            if src:
                section_media[_normalize_url(src)] = {
                    "section": sec_key,
                    "sectionLabel": sec.get("sectionLabel", ""),
                    "role": m.get("role", "content-image"),
                    "displayWidth": m.get("width"),
                    "displayHeight": m.get("height"),
                }

    for img in assets.get("images", []):
        url = img.get("url", "")
        entry = {
            "url": url,
            "filename": img.get("filename", ""),
            "path": img.get("path", ""),
        }
        match = section_media.get(_normalize_url(url), {})
        entry["section"] = match.get("section")
        entry["sectionLabel"] = match.get("sectionLabel")
        entry["role"] = match.get("role", _guess_image_role(url, img.get("filename", "")))
        entry["displayWidth"] = match.get("displayWidth")
        entry["displayHeight"] = match.get("displayHeight")
        manifest["images"].append(entry)

    for svg in assets.get("svgs", []):
        manifest["svgs"].append({
            "url": svg.get("url", ""),
            "filename": svg.get("filename", ""),
            "path": svg.get("path", ""),
            "type": "inline" if svg.get("url") == "inline" else "external",
        })

    for font in assets.get("fonts", []):
        manifest["fonts"].append({
            "url": font.get("url", ""),
            "filename": font.get("filename", ""),
            "path": font.get("path", ""),
            "type": font.get("type", "custom"),
        })

    for vid in assets.get("videos", []):
        entry = {
            "url": vid.get("url", ""),
            "filename": vid.get("filename", ""),
            "path": vid.get("path", ""),
        }
        match = section_media.get(_normalize_url(vid.get("url", "")), {})
        entry["section"] = match.get("section")
        entry["role"] = match.get("role", "video")
        manifest["videos"].append(entry)

    for j in assets.get("json", []):
        manifest["json"].append({
            "filename": j.get("filename", ""),
            "path": j.get("path", ""),
            "source": j.get("source", "unknown"),
        })

    return manifest


def _normalize_url(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/").lower()


def _guess_image_role(url: str, filename: str) -> str:
    lower = (url + filename).lower()
    if "logo" in lower:
        return "logo"
    if "icon" in lower or "favicon" in lower:
        return "icon"
    if "hero" in lower or "banner" in lower:
        return "hero-image"
    if "avatar" in lower or "profile" in lower:
        return "avatar"
    if "thumb" in lower:
        return "thumbnail"
    if "bg" in lower or "background" in lower:
        return "background"
    return "content-image"


# ---------------------------------------------------------------------------
# Site Metadata
# ---------------------------------------------------------------------------
def _build_site_metadata(raw_meta: dict, site_data: dict, network_data: dict | None,
                         brand_data: dict | None = None) -> dict:
    meta = {
        "title": raw_meta.get("title") or site_data.get("title", ""),
        "description": raw_meta.get("metaTags", {}).get("description", ""),
        "lang": raw_meta.get("lang"),
        "charset": raw_meta.get("charset"),
        "canonical": raw_meta.get("canonical"),
        "favicon": raw_meta.get("favicon"),
        "themeColor": raw_meta.get("themeColor"),
    }

    meta["og"] = raw_meta.get("og", {})
    meta["navigation"] = raw_meta.get("navigation", [])
    meta["preloads"] = raw_meta.get("preloads", [])

    meta["pages"] = site_data.get("pages", [])
    meta["category"] = site_data.get("category", "")
    meta["techStack"] = site_data.get("tech_stack", {})
    meta["platforms"] = site_data.get("platforms", {})
    meta["cms"] = site_data.get("cms", {})
    meta["cssTools"] = site_data.get("css_tools", {})

    if brand_data:
        meta["brand"] = {
            "siteName": brand_data.get("site_name"),
            "logo": brand_data.get("logo"),
            "favicons": brand_data.get("favicons", []),
            "themeColor": brand_data.get("theme_color"),
        }

    if network_data:
        meta["network"] = {
            "totalRequests": network_data.get("total_requests"),
            "totalTransferKB": network_data.get("total_transfer_size_kb"),
            "thirdPartyRequests": network_data.get("third_party_requests"),
            "cdnDetected": network_data.get("cdn_detected", []),
        }

    return meta


# ---------------------------------------------------------------------------
# Animation Map
# ---------------------------------------------------------------------------
def _build_animation_map(animation_data: dict) -> dict:
    elements = {}

    def _add_animation(selector: str, entry: dict):
        if not selector:
            return
        if selector not in elements:
            elements[selector] = {"selector": selector, "animations": []}
        elements[selector]["animations"].append(entry)

    for anim in animation_data.get("animations", []):
        target = anim.get("target", {})
        selector = _build_selector_from_target(target)
        _add_animation(selector, {
            "trigger": anim.get("trigger", "load"),
            "type": anim.get("type", "animation"),
            "duration_ms": anim.get("duration_ms"),
            "easing": anim.get("easing", "ease"),
            "delay_ms": anim.get("delay_ms", 0),
            "source": anim.get("source", "css"),
        })

    for hs in animation_data.get("hover_states", []):
        selector = hs.get("selector", "")
        if not selector:
            continue
        hover = hs.get("hover", {})
        trans = hs.get("transition") or {}
        if hover:
            _add_animation(selector, {
                "trigger": "hover",
                "changes": hover,
                "duration_ms": trans.get("transition_duration_ms", 300),
                "easing": trans.get("transition_easing", "ease"),
                "source": "hover-state",
            })

    for ct in animation_data.get("computed_transitions", []):
        target = ct
        selector = _build_selector_from_target(target)
        dur = ct.get("transitionDurationMs", 0)
        if dur and dur > 0:
            _add_animation(selector, {
                "trigger": "transition",
                "property": ct.get("transitionProperty", "all"),
                "duration_ms": dur,
                "easing": ct.get("transitionTimingFunction", "ease"),
                "source": "computed-transition",
            })

    ix2 = animation_data.get("webflow_ix2")
    if ix2 and isinstance(ix2, dict):
        for ix in ix2.get("interactions", []):
            trigger_type = ix.get("triggerType", "ix2")
            for timing in (ix.get("timings") or []):
                target_info = timing.get("target") or {}
                selector = target_info.get("selector")
                if not selector:
                    selector = target_info.get("useEventTarget") or f"ix2-{ix.get('id', '?')}"
                _add_animation(selector, {
                    "trigger": f"ix2:{trigger_type}",
                    "actionType": timing.get("actionTypeId"),
                    "duration_ms": timing.get("duration"),
                    "delay_ms": timing.get("delay", 0),
                    "easing": timing.get("easing"),
                    "values": {k: timing[k] for k in ("value", "xValue", "yValue", "widthValue", "heightValue") if timing.get(k) is not None},
                    "source": "webflow-ix2",
                })

    global_config = {}
    gsap = animation_data.get("gsap_config", {})
    if gsap.get("version"):
        global_config["gsap"] = {"version": gsap["version"], "defaults": gsap.get("defaults", {})}
    lenis = animation_data.get("lenis_config", {})
    if lenis and any(lenis.values()):
        global_config["lenis"] = lenis
    gmr = animation_data.get("global_motion_rules", {})
    if gmr:
        global_config["motionRules"] = gmr
    keyframes = animation_data.get("keyframe_definitions", [])
    if keyframes:
        global_config["keyframes"] = keyframes

    return {"elements": elements, "globalConfig": global_config}


def _build_selector_from_target(target: dict) -> str:
    if not target:
        return ""
    if isinstance(target, str):
        return target
    selector = target.get("selector")
    if selector:
        return selector
    tag = target.get("tag", "")
    el_id = target.get("id", "")
    classes = target.get("classes", [])
    if el_id:
        return f"{tag}#{el_id}" if tag else f"#{el_id}"
    if classes:
        cls_str = ".".join(c for c in classes[:3] if c)
        return f"{tag}.{cls_str}" if tag else f".{cls_str}"
    return tag or ""
