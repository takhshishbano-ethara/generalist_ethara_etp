"""
Phase 6B: PRD Writer.

Formats extracted website data into a structured prompt for LLM consumption.
The system prompt (PRD agent spec) is NOT included here — Odoo provides
it separately as the LLM system message.  This module only builds the
user message containing extraction data.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def build_prd_prompt(
    site_data: dict,
    style_data: dict,
    animation_data: dict,
    responsive_data: dict,
    asset_data: dict,
    webgl_data: dict | None = None,
    network_data: dict | None = None,
    performance_data: dict | None = None,
    auth_data: dict | None = None,
    brand_data: dict | None = None,
    component_tokens: dict | None = None,
    dark_mode_data: dict | None = None,
    output_dir: str | None = None,
) -> str:
    """
    Build the complete LLM prompt for generating a PRD.
    Returns a single string ready to feed to Claude/GPT.
    """
    parts = ["## EXTRACTED DATA\n"]

    # Document metadata
    parts.append(_format_metadata(site_data))

    # Visual identity data
    parts.append(_format_visual_identity(style_data))

    # Brand identity
    if brand_data:
        parts.append(_format_brand_data(brand_data))

    # Component design tokens
    if component_tokens:
        parts.append(_format_component_tokens(component_tokens))

    # Dark mode
    if dark_mode_data and dark_mode_data.get("has_dark_mode"):
        parts.append(_format_dark_mode(dark_mode_data))

    # Animation data
    parts.append(_format_animations(animation_data))

    # WebGL/3D data
    if webgl_data and webgl_data.get("detected"):
        parts.append(_format_webgl(webgl_data, site_data.get("category", "")))

    # Tech stack
    parts.append(_format_tech_stack(site_data, webgl_data))

    # Auth data
    parts.append(_format_auth(auth_data))

    # Network/API data
    if network_data:
        parts.append(_format_network(network_data))

    # Responsive data
    parts.append(_format_responsive(responsive_data))

    # Performance data
    if performance_data:
        parts.append(_format_performance(performance_data))

    # Assets reference
    parts.append(_format_assets(asset_data))

    # Pages discovered
    parts.append(_format_pages(site_data))

    # Screenshot references for Section 9
    parts.append(_format_screenshot_references(output_dir))

    # Final instruction
    parts.append("\n\n---\n\n## WRITE THE PRD NOW\n")
    parts.append("Using ONLY the extracted data above, write the complete PRD following the exact SOP skeleton structure in the system prompt.\n")
    # Strong word-count contract — Bedrock under-delivers (often ~3000)
    # without an emphatic instruction at the user-message level. Kept in
    # sync with Odoo's services/scoring_service.PRD_MAX_WORDS and
    # qc_service ranges (4000-5000) — anything under 4000 is rejected by
    # automated QC.
    parts.append(
        f"WORD COUNT CONTRACT (HARD): produce {_word_target(site_data)} words "
        f"(range 4,000-5,000). 5,000 is a hard ceiling, NEVER exceed. "
        f"PRDs under 4,000 words are flagged BELOW TARGET by automated "
        f"QC and rejected. Use the full word budget — comprehensive over "
        f"concise. Every value concrete and specific.\n"
    )
    parts.append("""
REMINDERS:
- Start with the metadata block (Project, Category, Version, URL) then a 2-3 sentence framing statement BEFORE Section 1
- Use all 10 sections as defined in the skeleton (Product Overview through Reference Prototypes)
- Include the Category Addendum after Section 5 (Motion Language) — this is REQUIRED for all categories
- Use numbered sub-sections (1.1, 1.2, 2.1, 2.2, etc.) as defined in the skeleton
- Open with a Product Overview that captures what this site IS and who it's for — infer from the URL, title, content, and tech stack
- Give every color a brand-evocative NAME plus its hex code: "Noir | #0A0A0A" not "Primary Background | #000000". Never use role labels (Primary, Secondary, Accent, Surface) as the name — invent a name that evokes the color (Bone, Brass, Charcoal, Papaya, Ink, Midnight)
- If font names look like internal slugs (e.g., "Ppneuemontrealvariable"), identify the likely real name (e.g., "PP Neue Montreal")
- EVERY page must follow the A–F structure (Entry Sequence, Layout, Content, Interactions, Special Effects, Exit Transition)
- Each page must have a DISTINCT description — never repeat "Standard content page" for multiple routes
- Data model must reflect the ACTUAL site content (products, projects, articles, etc.) — not generic Page/Section/MediaAsset
- For 0ms animations: these are GSAP-controlled, not CSS. Describe the likely GSAP tween behavior (300-900ms, power2.out)
- Include constraint language: "never X", "only for Y", "forbidden: Z" in color, layout, and motion sections
- Section 8 (Responsive Behavior): MUST have breakpoint table with Desktop 1440px, Tablet 1024px, Tablet 768px, Mobile 375px
- Section 10 (Reference Prototypes): describe the aesthetic target and list the reference screenshots
- The Motion Language section should read as a unified design system, not a table dump
""")

    return "\n".join(parts)


def _word_target(site_data: dict) -> str:
    return "5000"


def _format_metadata(site_data: dict) -> str:
    title = site_data.get("title", "Unknown")
    url = site_data.get("url", "")
    category = site_data.get("category", "Normal Website")
    return f"""
### Document Metadata
- **Project:** {title}
- **Category:** {category}
- **Version:** 1.0
- **URL:** {url}
"""


def _format_visual_identity(style_data: dict) -> str:
    parts = ["### Visual Identity Data\n"]

    # Colors
    colors = style_data.get("colors", [])
    if colors:
        parts.append("**Extracted Colors (by frequency):**\n")
        parts.append("| Hex | Usage Count | CSS Properties | Assign Brand-Evocative Name (NOT generic roles) |")
        parts.append("|-----|------------|----------------|------------------------------------------------|")
        for c in colors[:15]:
            usages = ", ".join(c.get("usages", []))
            parts.append(f"| {c['hex']} | {c['count']} | {usages} | (invent a name: Noir, Bone, Brass, Ink, etc.) |")
    parts.append("")

    # Fonts
    fonts = style_data.get("fonts", [])
    if fonts:
        parts.append("**Detected Fonts:**\n")
        parts.append("| Font Family | Weights | Usage Count |")
        parts.append("|-------------|---------|-------------|")
        for f in fonts[:10]:
            weights = ", ".join(str(w) for w in f.get("weights", []))
            parts.append(f"| {f['family']} | {weights} | {f['count']} |")
    parts.append("")

    # Font faces
    font_faces = style_data.get("font_faces", [])
    if font_faces:
        parts.append("**@font-face Declarations:**")
        for ff in font_faces[:10]:
            parts.append(f"- {ff.get('family', 'Unknown')} ({ff.get('weight', 'normal')} {ff.get('style', 'normal')})")
    parts.append("")

    # Type scale
    type_scale = style_data.get("type_scale", [])
    if type_scale:
        parts.append("**Type Scale:**\n")
        parts.append("| Size | Line Height | Letter Spacing | Font | Weight | Tags |")
        parts.append("|------|------------|----------------|------|--------|------|")
        for ts in type_scale[:10]:
            tags = ", ".join(ts.get("tags", []))
            parts.append(f"| {ts['size_px']}px | {ts.get('line_height', 'normal')} | {ts.get('letter_spacing', '0')} | {ts.get('font_family', '')} | {ts.get('font_weight', '')} | {tags} |")
    parts.append("")

    # Gradients
    gradients = style_data.get("gradients", [])
    if gradients:
        parts.append("**Detected Gradients:**")
        for g in gradients[:5]:
            parts.append(f"- `{g['value'][:150]}` (used {g['count']}x on {g.get('element', '')})")
    parts.append("")

    # Shadows
    shadows = style_data.get("shadows", [])
    if shadows:
        parts.append("**Box Shadows:**")
        for s in shadows[:5]:
            parts.append(f"- `{s['value'][:100]}` (used {s['count']}x)")
    parts.append("")

    # Border radii
    radii = style_data.get("border_radii", [])
    if radii:
        parts.append("**Border Radius Tokens:**")
        for r in radii[:8]:
            parts.append(f"- `{r['value']}` (used {r['count']}x)")
    parts.append("")

    # Effects
    effects = style_data.get("effects", {})
    if effects:
        parts.append("**CSS Effects:**")
        for prop, items in effects.items():
            if items:
                parts.append(f"- **{prop}:** {items[0]['value'][:80]} on {items[0].get('element', '')}")
    parts.append("")

    # Grid/layout
    grid = style_data.get("grid", {})
    if grid.get("max_widths"):
        parts.append(f"**Container Max-Widths:** {', '.join(str(int(w)) + 'px' for w in grid['max_widths'][:5])}")
    if grid.get("layouts"):
        parts.append("**Grid Layouts:**")
        for g in grid["layouts"][:5]:
            parts.append(f"- {g.get('element', '')}: columns=`{g.get('columns', '')}`, gap=`{g.get('gap', '')}`")
    parts.append("")

    # Spacing
    spacing = style_data.get("spacing", {})
    if spacing.get("values"):
        parts.append(f"**Spacing Values:** {', '.join(str(v) + 'px' for v in spacing['values'][:15])}")
        if spacing.get("baseline_unit"):
            parts.append(f"**Baseline Grid Unit:** {spacing['baseline_unit']}px")
    parts.append("")

    # CSS variables
    css_vars = style_data.get("css_variables", {})
    if css_vars:
        parts.append(f"**CSS Custom Properties ({len(css_vars)} detected):**")
        for name, value in list(css_vars.items())[:20]:
            parts.append(f"- `{name}`: `{value}`")
    parts.append("")

    # SEO
    seo = style_data.get("seo", {})
    if seo:
        parts.append("**SEO Metadata:**")
        for key, val in seo.items():
            if key != "json_ld" and val:
                parts.append(f"- {key}: {str(val)[:100]}")

    return "\n".join(parts)


def _format_animations(animation_data: dict) -> str:
    parts = ["### Animation & Interaction Data\n"]

    # Animations
    animations = animation_data.get("animations", [])
    if animations:
        parts.append(f"**CSS/Web Animations ({len(animations)} detected):**")
        for a in animations[:15]:
            target = a.get("target") or {}
            tag = target.get("tag", "?")
            classes = ".".join(target.get("classes", [])[:2])
            el_name = f"{tag}.{classes}" if classes else tag
            dur = a.get("duration_ms") or a.get("duration", "?")
            dur_str = f"{int(dur)}ms" if isinstance(dur, (int, float)) and dur > 0 else "?ms"
            trigger = a.get("trigger", "load")
            parts.append(f"- {el_name} ({a.get('type', 'animation')}): {dur_str}, easing={a.get('easing', '?')}, iterations={a.get('iterations', '?')}, trigger={trigger}")
    else:
        parts.append("**CSS/Web Animations:** None detected (site may use canvas/WebGL rendering)")
    parts.append("")

    # Hover states
    hover_states = animation_data.get("hover_states", [])
    if hover_states:
        has_css_changes = any(h.get("hover") for h in hover_states)
        if has_css_changes:
            parts.append("**Hover States with CSS Changes:**")
            for h in hover_states[:10]:
                if h.get("hover"):
                    parts.append(f"- `{h.get('selector', '')}`: transition={h.get('default', {}).get('transitionDuration', '0ms')}")
                    for prop, val in (h.get("hover") or {}).items():
                        def_val = (h.get("default") or {}).get(prop, "")
                        if val != def_val:
                            parts.append(f"  - {prop}: `{def_val}` → `{val}`")
        else:
            parts.append("**Hover States:** All interactive elements use JS event listeners (not CSS transitions) — likely canvas/WebGL-driven")
            parts.append(f"  - {len(hover_states)} interactive elements detected")
    parts.append("")

    # Keyframe definitions
    keyframes = animation_data.get("keyframe_definitions", [])
    if keyframes:
        parts.append("**CSS Keyframe Definitions:**")
        for kf in keyframes[:10]:
            parts.append(f"- `@keyframes {kf.get('name', '')}`: {kf.get('frame_count', '?')} frames")
    parts.append("")

    # Scroll triggers
    scroll_map = animation_data.get("scroll_animation_map", {})
    if scroll_map:
        parts.append("**Scroll-Triggered Animations:**")
        for pos, anims in list(scroll_map.items())[:10]:
            parts.append(f"- At {pos}%: {len(anims)} animation(s)")
    parts.append("")

    # Micro-interactions
    micro = animation_data.get("micro_interactions", [])
    if micro:
        parts.append("**Micro-Interactions:**")
        for m in micro[:10]:
            parts.append(f"- {m.get('element', '')}: {m.get('event', '')} → {m.get('description', '')}")

    return "\n".join(parts)


def _format_webgl(webgl_data: dict, category: str) -> str:
    parts = ["### 3D / WebGL Data\n"]

    canvases = webgl_data.get("canvases", [])
    if canvases:
        parts.append("**WebGL Canvases:**\n")
        parts.append("| # | Size | CSS Size | Pixel Ratio | Context | Antialias |")
        parts.append("|---|------|----------|-------------|---------|-----------|")
        for c in canvases:
            parts.append(f"| {c['index']} | {c['width']}x{c['height']} | {c['cssWidth']}x{c['cssHeight']} | {c.get('pixelRatio', 1)} | {c.get('contextType', '?')} | {c.get('antialias', False)} |")
    parts.append("")

    # GPU
    info = webgl_data.get("webgl_info", {})
    if info:
        parts.append(f"**GPU:** {info.get('gpuRenderer', 'Unknown')}")
        parts.append(f"**Max Texture Size:** {info.get('maxTextureSize', 'Unknown')}")
    parts.append("")

    # Three.js
    three = webgl_data.get("three_js")
    if three:
        parts.append(f"**Three.js:** r{three.get('revision', 'unknown')}")
        if three.get("renderer"):
            r = three["renderer"]
            parts.append(f"- Renderer: {r.get('type', 'Unknown')}")
            if r.get("dataEngine"):
                parts.append(f"- Data Engine: {r['dataEngine']}")
            if r.get("shadowMapEnabled"):
                parts.append(f"- Shadow Map: enabled (type={r.get('shadowMapType', '?')})")
            if r.get("toneMapping"):
                parts.append(f"- Tone Mapping: {r['toneMapping']} (exposure={r.get('toneMappingExposure', '?')})")
        if three.get("controls"):
            parts.append(f"- Controls: {three['controls']}")
        if three.get("physics"):
            parts.append(f"- Physics Engine: {three['physics']}")
        if three.get("post_processing"):
            parts.append(f"- Post-Processing: {', '.join(three['post_processing'])}")
        if three.get("loaders"):
            parts.append(f"- Loaders: {', '.join(three['loaders'])}")
        if three.get("code_hints"):
            parts.append(f"- Detected Features: {', '.join(three['code_hints'])}")
    parts.append("")

    # 3D Assets
    assets_3d = webgl_data.get("detected_3d_assets") or webgl_data.get("detected3DAssets", {})
    if assets_3d:
        parts.append("**3D Asset Files:**")
        for atype, files in assets_3d.items():
            parts.append(f"- {atype}: {len(files)} file(s)")
            for f in files[:3]:
                parts.append(f"  - {f}")
    parts.append("")

    # R3F
    if webgl_data.get("r3f_likely"):
        parts.append("**React Three Fiber (R3F):** Likely detected")

    parts.append("")
    parts.append("**IMPORTANT:** For 3D/WebGL sites, Section 3 (Animation) MUST cover the 3D rendering pipeline, camera setup, lighting, materials, and physics — not just CSS animations.")

    return "\n".join(parts)


def _format_tech_stack(site_data: dict, webgl_data: dict | None) -> str:
    parts = ["### Technical Stack Data\n"]

    tech = site_data.get("tech_stack", {})
    if tech:
        parts.append("**Detected Technologies:**\n")
        parts.append("| Library | Version | Type |")
        parts.append("|---------|---------|------|")
        for name, info in tech.items():
            version = info.get("version", "detected") if isinstance(info, dict) else "detected"
            lib_type = info.get("type", "unknown") if isinstance(info, dict) else "unknown"
            parts.append(f"| {name} | {version} | {lib_type} |")
    parts.append("")

    # Platforms
    platforms = site_data.get("platforms", {})
    if platforms:
        parts.append("**Platforms:**")
        for name, info in platforms.items():
            parts.append(f"- {name}: {info.get('evidence', 'detected')}")
    parts.append("")

    # CMS
    cms = site_data.get("cms", {})
    if cms:
        parts.append("**CMS:**")
        for name, info in cms.items():
            parts.append(f"- {name}: {info.get('evidence', 'detected')}")
    parts.append("")

    # CSS tools
    css_tools = site_data.get("css_tools", {})
    if css_tools:
        parts.append("**CSS Tools:**")
        for name, info in css_tools.items():
            parts.append(f"- {name}: confidence={info.get('confidence', 'detected')}")

    return "\n".join(parts)


def _format_auth(auth_data: dict | None) -> str:
    parts = ["### Authentication & User Data\n"]

    if not auth_data or not auth_data.get("has_auth"):
        parts.append("**Authentication:** No authentication detected on the site.")
        parts.append("**Note:** PRD must still define user roles (at minimum: Visitor, Admin, Content Manager) and explicitly state 'No authentication required' in the auth section.")
        return "\n".join(parts)

    if auth_data.get("login_forms"):
        parts.append("**Login Forms:**")
        for form in auth_data["login_forms"]:
            parts.append(f"- Action: {form.get('action', 'N/A')}, Method: {form.get('method', 'POST')}")
            for field in form.get("fields", []):
                parts.append(f"  - {field.get('type', '?')}: {field.get('name', '?')}")

    if auth_data.get("oauth_providers"):
        parts.append(f"**OAuth Providers:** {', '.join(auth_data['oauth_providers'])}")

    if auth_data.get("auth_meta", {}).get("sdk"):
        parts.append(f"**Auth SDK:** {auth_data['auth_meta']['sdk']}")

    if auth_data.get("cookie_consent"):
        parts.append(f"**Cookie Consent Banner:** Detected ({auth_data['cookie_consent'].get('selector', '')})")

    return "\n".join(parts)


def _format_network(network_data: dict) -> str:
    parts = ["### Network & API Data\n"]

    parts.append(f"**Total Requests:** {network_data.get('total_requests', '?')}")
    parts.append(f"**Total Transfer Size:** {network_data.get('total_transfer_size_kb', '?')} KB")
    parts.append(f"**Third-Party Requests:** {network_data.get('third_party_requests', '?')}")

    apis = network_data.get("api_patterns", [])
    if apis:
        parts.append("\n**API Endpoint Patterns (for data model):**\n")
        parts.append("| Pattern | Entity | Methods | Calls |")
        parts.append("|---------|--------|---------|-------|")
        for api in apis[:10]:
            methods = ", ".join(api.get("methods", []))
            parts.append(f"| {api['pattern']} | {api['entity']} | {methods} | {api['call_count']} |")

    cms = network_data.get("cms_detected", [])
    if cms:
        parts.append(f"\n**CMS Detected via Network:** {', '.join(cms)}")

    cdn = network_data.get("cdn_detected", [])
    if cdn:
        parts.append(f"**CDN/Hosting Detected:** {', '.join(cdn)}")

    if network_data.get("websocket_count", 0) > 0:
        parts.append(f"**WebSocket Connections:** {network_data['websocket_count']}")

    return "\n".join(parts)


def _format_responsive(responsive_data: dict) -> str:
    parts = ["### Responsive Behavior Data\n"]

    breakpoints = responsive_data.get("breakpoints", {})
    if breakpoints:
        parts.append("**Tested Breakpoints:**\n")
        parts.append("| Breakpoint | Width | Label |")
        parts.append("|-----------|-------|-------|")
        for name, bp in breakpoints.items():
            width = bp.get("width", "?")
            label = bp.get("label", name)
            parts.append(f"| {name} | {width}px | {label} |")
    parts.append("")

    sections = responsive_data.get("per_section_behavior", {})
    if sections:
        parts.append("**Per-Section Responsive Changes:**")
        for section_name, changes in sections.items():
            if section_name.startswith("_"):
                continue
            parts.append(f"\n**{section_name}:**")
            if isinstance(changes, dict):
                for bp_name, change in changes.items():
                    if isinstance(change, str):
                        parts.append(f"  - {bp_name}: {change}")
                    elif isinstance(change, dict):
                        for prop, val in change.items():
                            parts.append(f"  - {bp_name}: {prop} = {val}")
    parts.append("")

    typography = responsive_data.get("typography_changes", {})
    if typography:
        parts.append("**Typography Changes:**")
        for bp, changes in typography.items():
            if isinstance(changes, list):
                for c in changes:
                    parts.append(f"  - {bp}: {c}")
            elif isinstance(changes, str):
                parts.append(f"  - {bp}: {changes}")
    parts.append("")

    behaviors = responsive_data.get("behavior_changes", [])
    if behaviors:
        parts.append("**Behavior Changes:**")
        for b in behaviors[:10]:
            if isinstance(b, dict):
                parts.append(f"  - {b.get('breakpoint', '?')}: {b.get('change', '?')}")
            else:
                parts.append(f"  - {b}")

    return "\n".join(parts)


def _format_performance(performance_data: dict) -> str:
    parts = ["### Performance Data\n"]

    targets = performance_data.get("targets", {})
    if targets:
        parts.append("**Recommended Targets:**")
        for key, val in targets.items():
            parts.append(f"- {key}: {val}")
    parts.append("")

    measured = performance_data.get("measured", {})
    if measured:
        parts.append("**Measured Values:**")
        for key, val in measured.items():
            parts.append(f"- {key}: {val}")
    parts.append("")

    optimizations = performance_data.get("optimizations", [])
    if optimizations:
        parts.append("**Recommended Optimizations:**")
        for opt in optimizations:
            parts.append(f"- {opt}")
    parts.append("")

    parts.append(f"**prefers-reduced-motion in CSS:** {'Yes' if performance_data.get('reduced_motion_handled') else 'Not detected'}")

    a11y = performance_data.get("accessibility", {})
    if a11y:
        parts.append("\n**Accessibility Indicators:**")
        for key, val in a11y.items():
            parts.append(f"- {key}: {val}")

    return "\n".join(parts)


def _format_assets(asset_data: dict) -> str:
    parts = ["### Asset Reference\n"]

    screenshots = asset_data.get("screenshots", [])
    if screenshots:
        parts.append(f"**Screenshots ({len(screenshots)}):**")
        for s in screenshots:
            label = s.get("label", s.get("filename", ""))
            purpose = s.get("purpose", "")
            parts.append(f"- [{purpose}] {label}")
    parts.append("")

    assets = asset_data.get("assets", {})
    for category in ["images", "svgs", "fonts", "videos"]:
        items = assets.get(category, [])
        if items:
            parts.append(f"**{category.title()} ({len(items)}):**")
            for item in items[:10]:
                if isinstance(item, dict):
                    parts.append(f"- {item.get('filename', item.get('url', '?'))}")
                else:
                    parts.append(f"- {item}")

    return "\n".join(parts)


def _format_pages(site_data: dict) -> str:
    parts = ["### Discovered Pages\n"]

    pages = site_data.get("pages", [])
    if pages:
        for p in pages[:10]:
            if isinstance(p, dict):
                parts.append(f"- {p.get('title', p.get('url', '?'))}: {p.get('url', '')}")
            else:
                parts.append(f"- {p}")
    else:
        parts.append("- Single page application (no additional pages discovered)")

    return "\n".join(parts)


def _format_brand_data(brand_data: dict) -> str:
    parts = ["### Brand Identity Data\n"]

    if brand_data.get("site_name"):
        parts.append(f"**Site Name:** {brand_data['site_name']}")

    logo = brand_data.get("logo")
    if logo:
        parts.append(f"**Logo:** {logo.get('src', 'N/A')} (score={logo.get('score', '?')}, {logo.get('width', '?')}x{logo.get('height', '?')})")

    favicons = brand_data.get("favicons", [])
    if favicons:
        parts.append("**Favicons:**")
        for f in favicons[:5]:
            parts.append(f"- {f.get('href', '?')} (type={f.get('type', '?')}, sizes={f.get('sizes', '?')})")

    manifest = brand_data.get("manifest")
    if manifest:
        parts.append(f"**Web Manifest:** {manifest.get('name', 'N/A')} — theme_color={manifest.get('theme_color', 'N/A')}")

    og = brand_data.get("og", {})
    if og:
        parts.append("**Open Graph:**")
        for key, val in og.items():
            if val:
                parts.append(f"- og:{key}: {str(val)[:120]}")

    return "\n".join(parts)


def _format_component_tokens(component_tokens: dict) -> str:
    parts = ["### Component Design Tokens\n"]

    for comp_type in ["buttons", "inputs", "links", "badges"]:
        items = component_tokens.get(comp_type, [])
        if not items:
            continue
        parts.append(f"**{comp_type.title()} ({len(items)}):**")
        for item in items[:5]:
            selector = item.get("selector", "?")
            parts.append(f"\n`{selector}`:")
            default = item.get("default", {})
            if default:
                props = [f"{k}={v}" for k, v in list(default.items())[:6]]
                parts.append(f"  - default: {', '.join(props)}")
            for state in ["hover", "focus", "active"]:
                state_data = item.get(state, {})
                if state_data:
                    changes = [f"{k}={v}" for k, v in list(state_data.items())[:4]]
                    parts.append(f"  - {state}: {', '.join(changes)}")
        parts.append("")

    return "\n".join(parts)


def _format_dark_mode(dark_mode_data: dict) -> str:
    parts = ["### Dark Mode Data\n"]

    parts.append(f"**Detection Method:** {dark_mode_data.get('detection_method', 'N/A')}")

    overrides = dark_mode_data.get("css_variable_overrides", {})
    if overrides:
        parts.append(f"**CSS Variable Overrides ({len(overrides)}):**")
        for name, val in list(overrides.items())[:15]:
            parts.append(f"- `{name}`: `{val}`")

    color_changes = dark_mode_data.get("color_changes", 0)
    if color_changes:
        parts.append(f"**Total Color Changes:** {color_changes}")

    return "\n".join(parts)


def _format_screenshot_references(output_dir: str | None) -> str:
    parts = ["### Captured Screenshots (for Section 9: Reference Prototypes)\n"]

    if not output_dir:
        parts.append("No output directory provided — describe aesthetic target based on extracted color/typography data.")
        return "\n".join(parts)

    screenshots_dir = Path(output_dir) / "screenshots"
    if not screenshots_dir.exists():
        parts.append("No screenshots directory found — describe aesthetic target based on extracted color/typography data.")
        return "\n".join(parts)

    screenshot_files = sorted(screenshots_dir.glob("*.png"))
    if not screenshot_files:
        screenshot_files = sorted(screenshots_dir.glob("*.jpg")) + sorted(screenshots_dir.glob("*.webp"))

    if screenshot_files:
        parts.append(f"**Reference Screenshots ({len(screenshot_files)}):**")
        for f in screenshot_files:
            parts.append(f"- References/{f.name}")
        parts.append("")
        parts.append("Use these filenames in Section 9 to reference the design targets.")
    else:
        parts.append("No screenshot files found — describe aesthetic target based on extracted color/typography data.")

    return "\n".join(parts)
