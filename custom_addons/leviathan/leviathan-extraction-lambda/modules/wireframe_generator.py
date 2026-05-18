"""
Phase W: Wireframe Generator — Screenshot-based lo-fi wireframes + section maps.

Injects CSS to transform the live page into a grayscale wireframe appearance,
takes full-page PNG screenshots, then post-processes with Pillow for uniform
desaturation. Also generates section-map SVGs from DOM layout data.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PIL import Image, ImageEnhance
from playwright.async_api import Page

logger = logging.getLogger(__name__)

_JS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inject_dom_layout.js"

_WIREFRAME_CSS = """
*, *::before, *::after {
  color: #222 !important;
  border-color: #aaa !important;
  outline-color: #aaa !important;
  text-decoration-color: #666 !important;
  box-shadow: none !important;
  text-shadow: none !important;
  background-image: none !important;
  transition: none !important;
  animation: none !important;
  animation-play-state: paused !important;
}

html, body {
  background-color: #f0f0f0 !important;
}

main, article, section, div, aside, footer, header, nav, form, fieldset {
  background-color: #f0f0f0 !important;
}

h1, h2, h3, h4, h5, h6 {
  color: #111 !important;
}

p, span, li, td, th, label, figcaption {
  color: #333 !important;
}

img, picture, video, iframe {
  filter: grayscale(1) contrast(0.5) !important;
  opacity: 0.55 !important;
}

svg {
  filter: grayscale(1) contrast(0.6) !important;
  opacity: 0.6 !important;
}

canvas {
  filter: grayscale(1) contrast(0.3) !important;
  opacity: 0.4 !important;
}

section, article, header, footer, nav, main, aside,
[class*="section"], [class*="container"], [class*="wrapper"],
[class*="Section"], [class*="Container"], [class*="Wrapper"] {
  border: 2px solid #bbb !important;
}

a, button, [role="button"] {
  border: 2px solid #999 !important;
  background-color: #ddd !important;
  color: #111 !important;
}

input, select, textarea {
  border: 2px solid #999 !important;
  background-color: #fff !important;
  color: #222 !important;
}

[aria-hidden="true"]:not(svg):not([class*="nav"]):not([class*="menu"]) {
  opacity: 0.25 !important;
}

::placeholder {
  color: #777 !important;
}
"""

_WIREFRAME_CLEANUP_JS = """
(() => {
  // Pause all Web Animations
  try { document.getAnimations().forEach(a => a.pause()); } catch(e) {}

  // Pause all videos
  document.querySelectorAll('video').forEach(v => {
    try { v.pause(); v.currentTime = 0; } catch(e) {}
  });

  // Replace canvas elements with gray placeholders
  document.querySelectorAll('canvas').forEach(c => {
    try {
      const rect = c.getBoundingClientRect();
      if (rect.width < 10 || rect.height < 10) return;
      const placeholder = document.createElement('div');
      placeholder.style.cssText =
        'width:' + rect.width + 'px;height:' + rect.height + 'px;' +
        'background:#e0e0e0;border:1px dashed #bbb;' +
        'display:flex;align-items:center;justify-content:center;' +
        'color:#999;font-family:Arial,sans-serif;font-size:14px;';
      placeholder.textContent = '[Canvas / WebGL]';
      if (c.parentNode) c.parentNode.replaceChild(placeholder, c);
    } catch(e) {}
  });

  // Force-stop any GSAP animations
  try { if (window.gsap) window.gsap.globalTimeline.pause(); } catch(e) {}

  // Scroll to top
  window.scrollTo(0, 0);
})();
"""

MAX_SCREENSHOT_HEIGHT = 8000

SECTION_COLORS = [
    "#E3F2FD", "#E8F5E9", "#FFF3E0", "#F3E5F5",
    "#FFEBEE", "#E0F7FA", "#FFF8E1", "#E8EAF6",
]

ROLE_STYLES = {
    "nav":         {"fill": "#E3F2FD", "stroke": "#90CAF9", "pattern": None},
    "header":      {"fill": "#E8F5E9", "stroke": "#A5D6A7", "pattern": None},
    "footer":      {"fill": "#F3E5F5", "stroke": "#CE93D8", "pattern": None},
    "main":        {"fill": "#FAFAFA", "stroke": "#BDBDBD", "pattern": None},
    "sidebar":     {"fill": "#FFF8E1", "stroke": "#FFE082", "pattern": None},
    "section":     {"fill": "#FAFAFA", "stroke": "#BDBDBD", "pattern": None},
    "article":     {"fill": "#FAFAFA", "stroke": "#BDBDBD", "pattern": None},
    "hero":        {"fill": "#E8EAF6", "stroke": "#9FA8DA", "pattern": None},
    "heading":     {"fill": "#424242", "stroke": "none", "pattern": "text"},
    "text":        {"fill": "#9E9E9E", "stroke": "none", "pattern": "lines"},
    "media":       {"fill": "#EEEEEE", "stroke": "#BDBDBD", "pattern": "cross"},
    "canvas":      {"fill": "#E0E0E0", "stroke": "#BDBDBD", "pattern": "diagonal"},
    "interactive": {"fill": "#E3F2FD", "stroke": "#64B5F6", "pattern": "button"},
    "form-field":  {"fill": "#FFFFFF", "stroke": "#BDBDBD", "pattern": None},
    "form":        {"fill": "#FAFAFA", "stroke": "#BDBDBD", "pattern": None},
    "card":        {"fill": "#FFFFFF", "stroke": "#E0E0E0", "pattern": None},
    "list":        {"fill": "#FAFAFA", "stroke": "#E0E0E0", "pattern": None},
    "table":       {"fill": "#FAFAFA", "stroke": "#BDBDBD", "pattern": None},
    "grid":        {"fill": "#FAFAFA", "stroke": "#E0E0E0", "pattern": None},
    "carousel":    {"fill": "#FAFAFA", "stroke": "#90CAF9", "pattern": None},
    "modal":       {"fill": "#FFFFFF", "stroke": "#757575", "pattern": None},
    "menu":        {"fill": "#E3F2FD", "stroke": "#90CAF9", "pattern": None},
    "dropdown":    {"fill": "#FFFFFF", "stroke": "#BDBDBD", "pattern": None},
    "banner":      {"fill": "#FFF3E0", "stroke": "#FFB74D", "pattern": None},
    "search":      {"fill": "#FFFFFF", "stroke": "#BDBDBD", "pattern": None},
    "logo":        {"fill": "#E0E0E0", "stroke": "#BDBDBD", "pattern": "cross"},
    "icon":        {"fill": "#BDBDBD", "stroke": "none", "pattern": None},
    "figure":      {"fill": "#EEEEEE", "stroke": "#BDBDBD", "pattern": "cross"},
    "quote":       {"fill": "#F5F5F5", "stroke": "#BDBDBD", "pattern": None},
    "container":   {"fill": "none", "stroke": "#E0E0E0", "pattern": None},
}


async def generate_wireframes(page: Page, output_dir: str, viewports: list[dict] | None = None) -> dict:
    """Generate wireframe SVGs at multiple viewport sizes (Phase 7, non-destructive)."""
    if viewports is None:
        viewports = [
            {"width": 1920, "height": 1080, "name": "desktop_1920"},
            {"width": 768, "height": 1024, "name": "tablet_768"},
            {"width": 375, "height": 812, "name": "mobile_375"},
        ]

    wireframe_dir = os.path.join(output_dir, "wireframes")
    os.makedirs(wireframe_dir, exist_ok=True)

    js = _JS_PATH.read_text(encoding="utf-8")
    results = {}
    original_viewport = page.viewport_size

    for vp in viewports:
        try:
            await page.set_viewport_size({"width": vp["width"], "height": vp["height"]})
            await page.wait_for_timeout(500)

            layout_data = await page.evaluate(js)
            if not layout_data:
                logger.warning("No layout data for viewport %s", vp["name"])
                continue

            tree = layout_data.get("tree")
            sections = layout_data.get("sections", [])
            page_height = layout_data.get("pageHeight", 2000)
            is_canvas_heavy = _detect_canvas_heavy(tree, sections, vp)

            section_map_path = os.path.join(wireframe_dir, f"{vp['name']}_sections.svg")

            if is_canvas_heavy:
                _render_canvas_wireframe_svg(layout_data, section_map_path, vp["width"], vp["height"])
            elif tree:
                _render_tree_wireframe_svg(tree, page_height, section_map_path, vp["width"], vp["height"])
            else:
                _render_section_map_svg(layout_data, section_map_path, vp["width"])

            results[vp["name"]] = {
                "section_map": section_map_path,
                "viewport": vp,
                "page_height": page_height,
                "sections": len(sections),
            }
        except Exception as exc:
            logger.warning("Wireframe generation failed for %s: %s", vp["name"], exc, exc_info=True)

    if original_viewport:
        await page.set_viewport_size(original_viewport)

    return results


def _detect_canvas_heavy(tree: dict | None, sections: list, vp: dict) -> bool:
    """Check if the page is primarily a full-viewport canvas."""
    if not tree:
        return False
    for child in tree.get("children", []):
        if child.get("tag") == "canvas":
            cw = child.get("w", 0)
            ch = child.get("h", 0)
            coverage = (cw * ch) / max(vp["width"] * vp["height"], 1)
            if coverage > 0.7:
                return True
    if len(sections) <= 1:
        for s in sections:
            if s.get("tag") == "canvas":
                return True
    return False


async def generate_wireframe_screenshots(
    page: Page, output_dir: str, viewports: list[dict] | None = None
) -> dict:
    """
    Generate lo-fi wireframe PNGs by injecting CSS into the live page (run after perf analysis).

    Injects grayscale CSS, pauses animations, replaces canvases, takes full-page
    screenshots, then post-processes with Pillow for uniform desaturation.
    """
    if viewports is None:
        viewports = [
            {"width": 1920, "height": 1080, "name": "desktop_1920"},
            {"width": 768, "height": 1024, "name": "tablet_768"},
            {"width": 375, "height": 812, "name": "mobile_375"},
        ]

    wireframe_dir = os.path.join(output_dir, "wireframes")
    os.makedirs(wireframe_dir, exist_ok=True)

    results = {}
    original_viewport = page.viewport_size

    for vp in viewports:
        try:
            await page.set_viewport_size({"width": vp["width"], "height": vp["height"]})
            await page.wait_for_timeout(300)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(200)

            style_handle = await page.add_style_tag(content=_WIREFRAME_CSS)
            await page.evaluate(_WIREFRAME_CLEANUP_JS)
            await page.wait_for_timeout(500)

            png_path = os.path.join(wireframe_dir, f"{vp['name']}.png")
            await page.screenshot(path=png_path, full_page=True)

            _postprocess_wireframe(png_path)

            results[vp["name"]] = {
                "wireframe_png": png_path,
                "viewport": vp,
            }

            logger.info("Wireframe screenshot saved: %s", png_path)

        except Exception as exc:
            logger.warning(
                "Wireframe screenshot failed for %s: %s", vp["name"], exc, exc_info=True
            )

    if original_viewport:
        try:
            await page.set_viewport_size(original_viewport)
        except Exception:
            pass

    return results


def _postprocess_wireframe(path: str):
    """Convert screenshot to uniform grayscale with strong readable contrast."""
    try:
        img = Image.open(path)
        w, h = img.size
        if h > MAX_SCREENSHOT_HEIGHT:
            img = img.crop((0, 0, w, MAX_SCREENSHOT_HEIGHT))
        gray = img.convert("L").convert("RGB")
        gray = ImageEnhance.Contrast(gray).enhance(1.3)
        gray = ImageEnhance.Brightness(gray).enhance(1.05)
        gray.save(path, optimize=True)
    except Exception as exc:
        logger.warning("Wireframe post-processing failed for %s: %s", path, exc)


def _svg_header(vw: int, page_h: int, max_h: int = 5000) -> tuple[int, int, list[str]]:
    """Return (svg_w, svg_h, lines) for an SVG with standard styles."""
    clamped_h = min(page_h, max_h)
    # Scale up small viewports so the SVG is at least 600px wide
    target_w = 800
    scale = target_w / vw
    svg_w = round(vw * scale)
    svg_h = round(clamped_h * scale)
    lines: list[str] = []
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" '
        f'viewBox="0 0 {vw} {clamped_h}">'
    )
    lines.append(f'  <rect width="{vw}" height="{clamped_h}" fill="#FAFAFA"/>')
    lines.append("  <defs>")
    # Cross-hatch pattern for images/media
    lines.append('    <pattern id="cross" width="12" height="12" patternUnits="userSpaceOnUse">')
    lines.append('      <path d="M0 0L12 12M12 0L0 12" stroke="#BDBDBD" stroke-width="0.5"/>')
    lines.append("    </pattern>")
    # Diagonal lines for canvas
    lines.append('    <pattern id="diagonal" width="8" height="8" patternUnits="userSpaceOnUse">')
    lines.append('      <path d="M0 8L8 0" stroke="#BDBDBD" stroke-width="0.5"/>')
    lines.append("    </pattern>")
    # Horizontal lines for text
    lines.append('    <pattern id="textlines" width="100" height="6" patternUnits="userSpaceOnUse">')
    lines.append('      <rect width="100" height="3" fill="#BDBDBD"/>')
    lines.append("    </pattern>")
    lines.append("  </defs>")
    lines.append("  <style>")
    lines.append("    text { font-family: Arial, Helvetica, sans-serif; }")
    lines.append('    .label { font-size: 13px; font-weight: 600; fill: #555; }')
    lines.append('    .meta { font-size: 10px; fill: #999; }')
    lines.append('    .heading-text { font-size: 14px; font-weight: 700; fill: #333; }')
    lines.append('    .btn-text { font-size: 11px; font-weight: 600; fill: #1565C0; }')
    lines.append("  </style>")
    return svg_w, svg_h, lines


def _render_tree_wireframe_svg(tree: dict, page_height: int, output_path: str, vw: int, viewport_h: int = 1080):
    """Render a full wireframe from the DOM layout tree with nested elements."""
    max_h = min(page_height, 5000)
    _, _, lines = _svg_header(vw, page_height)

    _render_node(tree, lines, depth=0, max_y=max_h, vw=vw, vh=viewport_h)

    lines.append("</svg>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _render_node(node: dict, lines: list[str], depth: int, max_y: int, vw: int = 0, vh: int = 0):
    """Recursively render a DOM tree node as SVG elements."""
    if not node:
        return

    x = node.get("x", 0)
    y = node.get("y", 0)
    w = node.get("w", 0)
    h = node.get("h", 0)
    role = node.get("role", "container")
    tag = node.get("tag", "div")
    text = node.get("text")
    position = node.get("position", "")

    if y > max_y or w < 5 or h < 3:
        return

    # Skip full-viewport background media (hero images, video backgrounds)
    if role in ("media", "figure") and vw > 0 and vh > 0:
        coverage = (w * h) / max(vw * vh, 1)
        if coverage > 0.5:
            for child in node.get("children", []):
                _render_node(child, lines, depth + 1, max_y, vw, vh)
            return

    style = ROLE_STYLES.get(role, ROLE_STYLES["container"])
    fill = style["fill"]
    stroke = style["stroke"]
    pattern = style["pattern"]

    indent = "  " * (depth + 1)

    # Draw element based on role
    if role == "heading" and text:
        bar_h = min(h, 20)
        lines.append(
            f'{indent}<rect x="{x}" y="{y}" width="{min(w, len(text) * 9 + 16)}" '
            f'height="{bar_h}" rx="2" fill="#424242"/>'
        )
        lines.append(
            f'{indent}<text x="{x + 8}" y="{y + bar_h - 5}" class="heading-text" fill="#FFF">'
            f'{_escape_xml(text[:40])}</text>'
        )

    elif role == "text":
        # Render as gray lines representing text
        num_lines = max(1, min(h // 8, 6))
        for i in range(num_lines):
            ly = y + 4 + i * 8
            line_w = w * (0.95 if i < num_lines - 1 else 0.6)
            lines.append(
                f'{indent}<rect x="{x}" y="{ly}" width="{line_w:.0f}" '
                f'height="4" rx="1" fill="#BDBDBD"/>'
            )

    elif role == "media" or role == "figure":
        lines.append(
            f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'fill="#EEEEEE" stroke="#BDBDBD" stroke-width="1"/>'
        )
        lines.append(
            f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="url(#cross)"/>'
        )
        # X through image
        lines.append(
            f'{indent}<line x1="{x}" y1="{y}" x2="{x + w}" y2="{y + h}" '
            f'stroke="#BDBDBD" stroke-width="0.5"/>'
        )
        lines.append(
            f'{indent}<line x1="{x + w}" y1="{y}" x2="{x}" y2="{y + h}" '
            f'stroke="#BDBDBD" stroke-width="0.5"/>'
        )

    elif role == "interactive":
        # Button / link
        rx = min(6, h // 2)
        lines.append(
            f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="{rx}" fill="#E3F2FD" stroke="#64B5F6" stroke-width="1.5"/>'
        )
        if text and h >= 16:
            lines.append(
                f'{indent}<text x="{x + w / 2}" y="{y + h / 2 + 4}" '
                f'text-anchor="middle" class="btn-text">{_escape_xml(text[:20])}</text>'
            )

    elif role == "form-field":
        lines.append(
            f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="4" fill="#FFF" stroke="#BDBDBD" stroke-width="1"/>'
        )

    elif role == "canvas":
        lines.append(
            f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'fill="#E0E0E0" stroke="#BDBDBD" stroke-width="1"/>'
        )
        lines.append(
            f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="url(#diagonal)"/>'
        )
        if w > 100 and h > 40:
            lines.append(
                f'{indent}<text x="{x + w / 2}" y="{y + h / 2 + 5}" '
                f'text-anchor="middle" class="label">[Canvas / WebGL]</text>'
            )

    elif role == "icon" or role == "logo":
        lines.append(
            f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="4" fill="#E0E0E0" stroke="#BDBDBD" stroke-width="0.5"/>'
        )

    elif role in ("nav", "header", "footer", "sidebar", "hero", "banner",
                   "card", "modal", "menu", "section", "article"):
        if fill != "none":
            lines.append(
                f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="1" rx="2"/>'
            )
        else:
            lines.append(
                f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" '
                f'fill="none" stroke="{stroke}" stroke-width="1" stroke-dasharray="4,2"/>'
            )
        # Label for structural elements
        if h > 24 and w > 60 and depth <= 2:
            lines.append(
                f'{indent}<text x="{x + 6}" y="{y + 14}" class="meta">{role}</text>'
            )

    elif role in ("grid", "list", "carousel", "table"):
        lines.append(
            f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'fill="none" stroke="{stroke}" stroke-width="1" stroke-dasharray="3,2"/>'
        )

    else:
        # Generic container — only draw if it has meaningful size at shallow depth
        if depth <= 2 and w > 100 and h > 50:
            lines.append(
                f'{indent}<rect x="{x}" y="{y}" width="{w}" height="{h}" '
                f'fill="none" stroke="#E0E0E0" stroke-width="0.5" stroke-dasharray="4,3"/>'
            )

    # Recurse into children
    for child in node.get("children", []):
        _render_node(child, lines, depth + 1, max_y, vw, vh)


def _render_canvas_wireframe_svg(layout_data: dict, output_path: str, vw: int, vh: int):
    """Render a synthetic wireframe for canvas/WebGL/game sites."""
    _, _, lines = _svg_header(vw, vh, max_h=vh)

    # Full canvas background
    lines.append(
        f'  <rect x="0" y="0" width="{vw}" height="{vh}" '
        f'fill="#E0E0E0" stroke="#BDBDBD" stroke-width="1"/>'
    )
    lines.append(
        f'  <rect x="0" y="0" width="{vw}" height="{vh}" fill="url(#diagonal)"/>'
    )
    lines.append(
        f'  <text x="{vw // 2}" y="{vh // 2}" text-anchor="middle" '
        f'class="label" font-size="18">[Canvas / WebGL — Full Viewport]</text>'
    )

    # Render any HTML overlay elements from the tree
    tree = layout_data.get("tree")
    if tree:
        for child in tree.get("children", []):
            if child.get("tag") == "canvas":
                continue
            _render_node(child, lines, depth=1, max_y=vh, vw=vw, vh=vh)

    # Synthetic game UI zones based on common patterns
    sections = layout_data.get("sections", [])
    html_elements_found = sum(1 for s in sections if s.get("tag") != "canvas")
    if html_elements_found < 2:
        margin = 20
        # Top bar — typical nav/HUD area
        lines.append(
            f'  <rect x="{margin}" y="{margin}" width="{vw - margin * 2}" height="50" '
            f'rx="4" fill="rgba(255,255,255,0.15)" stroke="#90CAF9" stroke-width="1" '
            f'stroke-dasharray="6,3"/>'
        )
        lines.append(
            f'  <text x="{margin + 10}" y="{margin + 30}" class="meta" fill="#666">Navigation / HUD</text>'
        )
        # Center — main interaction area
        cx = vw // 2
        cy = vh // 2
        iw = min(400, vw // 3)
        ih = min(200, vh // 4)
        lines.append(
            f'  <rect x="{cx - iw // 2}" y="{cy - ih // 2}" width="{iw}" height="{ih}" '
            f'rx="8" fill="rgba(255,255,255,0.1)" stroke="#90CAF9" stroke-width="1" '
            f'stroke-dasharray="6,3"/>'
        )
        lines.append(
            f'  <text x="{cx}" y="{cy + 5}" text-anchor="middle" class="label" fill="#555">'
            f'[Interactive 3D Scene]</text>'
        )
        # Bottom — controls / info
        lines.append(
            f'  <rect x="{margin}" y="{vh - 70}" width="{vw - margin * 2}" height="50" '
            f'rx="4" fill="rgba(255,255,255,0.15)" stroke="#90CAF9" stroke-width="1" '
            f'stroke-dasharray="6,3"/>'
        )
        lines.append(
            f'  <text x="{margin + 10}" y="{vh - 40}" class="meta" fill="#666">'
            f'Controls / Status Bar</text>'
        )
        # Social links / corner element
        lines.append(
            f'  <rect x="{vw - 160}" y="{margin}" width="140" height="50" '
            f'rx="4" fill="rgba(255,255,255,0.15)" stroke="#CE93D8" stroke-width="1" '
            f'stroke-dasharray="6,3"/>'
        )
        lines.append(
            f'  <text x="{vw - 150}" y="{margin + 30}" class="meta" fill="#666">Social / Links</text>'
        )

    lines.append("</svg>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _render_section_map_svg(layout_data: dict, output_path: str, viewport_width: int):
    """Fallback: simple section-level map when no tree is available."""
    sections = layout_data.get("sections", [])
    if not sections:
        return

    page_height = layout_data.get("pageHeight", 2000)
    _, _, lines = _svg_header(viewport_width, page_height)

    for i, section in enumerate(sections):
        x = section.get("x", 0)
        y = section.get("y", 0)
        w = section.get("w", viewport_width)
        h = section.get("h", 100)

        if h < 10 or y > 5000:
            continue

        color = SECTION_COLORS[i % len(SECTION_COLORS)]
        lines.append(
            f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'fill="{color}" stroke="#AAA" stroke-width="1"/>'
        )

        label = section.get("label", section.get("role", "section"))
        role = section.get("role", "")
        meta = f"{w}x{h}px"

        text_x = x + 10
        text_y = y + 24
        if h > 30:
            lines.append(
                f'  <text x="{text_x}" y="{text_y}" class="label">'
                f"{_escape_xml(label[:50])}</text>"
            )
        if h > 50:
            lines.append(
                f'  <text x="{text_x}" y="{text_y + 18}" class="meta">'
                f"{role} — {meta}</text>"
            )

    lines.append("</svg>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _escape_xml(text: str) -> str:
    """Escape special XML characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
