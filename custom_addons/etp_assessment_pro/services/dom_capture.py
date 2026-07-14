# -*- coding: utf-8 -*-
"""DOM-truth capture for image_label questions (Phase 4).

When an image_label source is a LIVE URL, we drive a headless Chromium via
Playwright to enumerate the page's interactive DOM elements at their real
geometry, screenshot the page, draw numbered boxes at the DOM rects, and
mechanically draft a BEHAVIOURAL answer key (what each element DOES) with ZERO
model inference — the boxes and the key are ground truth BY CONSTRUCTION, exactly
as the reference proj-2 asset generator does. When the source is not a URL, or
Playwright/Chromium is unavailable, the caller falls back to the Gemini
detection path unchanged.

Playwright is imported behind a try/except so importing THIS module NEVER
hard-fails when Playwright/Chromium is absent; callers gate on
``PLAYWRIGHT_AVAILABLE`` and degrade gracefully. Playwright is deliberately NOT
declared in the manifest's external_dependencies for the same reason.
"""
import datetime
import io
import logging

try:  # pragma: no cover - exercised only where Playwright is installed
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # the common case in this environment
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

_logger = logging.getLogger(__name__)

# Ported VERBATIM from the reference generator
# (dummy_testing/reference sop/agon-proj2-proj3/proj-2/run*/generate_assets.py,
# COLLECT_JS). Walks the composed tree (incl. shadow roots), keeps only visible,
# hit-testable interactive elements, boxes hidden checkbox/radio inputs at their
# driving label, drops nested duplicates, and returns each element's CSS-pixel
# rect + construction facts in reading order. No model inference whatsoever.
_COLLECT_JS = r"""
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const sels = 'a[href], button, input, select, textarea, summary, iframe, video, ' +
    '[role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], ' +
    '[role=radio], [role=switch], [role=combobox], [role=searchbox], [role=option], ' +
    'label[for], [onclick], [contenteditable=true]';

  // walk the composed tree so controls inside shadow roots are seen too;
  // bare anchors without href but styled as pointers are JS-driven controls
  // (carousel pagers work this way) and count as interactive
  const nodes = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.matches(sels) ||
          (el.tagName === 'A' && !el.hasAttribute('href') &&
           getComputedStyle(el).cursor === 'pointer')) nodes.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);

  const composedParent = (el) => {
    if (el.parentElement) return el.parentElement;
    const root = el.getRootNode();
    return root instanceof ShadowRoot ? root.host : null;
  };
  const composedContains = (target, node) => {
    while (node) {
      if (node === target) return true;
      node = composedParent(node);
    }
    return false;
  };
  const deepHit = (x, y) => {
    let hit = document.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) {
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    return hit;
  };
  const isHidden = (el) => {
    const cs = getComputedStyle(el);
    const rc = el.getBoundingClientRect();
    return rc.width < 4 || rc.height < 4 || cs.visibility === 'hidden' ||
           cs.display === 'none' || parseFloat(cs.opacity) < 0.05;
  };

  const cand = [];
  for (const el of nodes) {
    let target = el;
    const tag = el.tagName.toLowerCase();
    // label[for]: keep the label ONLY when its control is hidden (the label
    // drives a hidden input); otherwise the control itself is boxed, so skip
    if (tag === 'label' && el.getAttribute('for')) {
      const root = el.getRootNode();
      const ctl = (root.getElementById ? root.getElementById(el.getAttribute('for'))
                   : document.getElementById(el.getAttribute('for')));
      if (!ctl || !isHidden(ctl)) continue;
    }
    // hidden checkbox or radio driven by a visible label: box the label
    if (tag === 'input' && (el.type === 'checkbox' || el.type === 'radio')) {
      const st0 = getComputedStyle(el);
      if (st0.display === 'none' || st0.visibility === 'hidden' ||
          el.offsetParent === null || el.getBoundingClientRect().width < 2) {
        let lab = el.id ? el.getRootNode().querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
        if (!lab) lab = el.closest('label');
        if (lab) target = lab; else continue;
      }
    }
    const ts = getComputedStyle(target);
    if (ts.display === 'none' || ts.visibility === 'hidden') continue;
    // keep an icon checkbox/radio even at opacity 0 (it is the real click
    // surface styled by an overlay); opacity-0 native select/input are kept for
    // the same reason (the wikipedia.org language select works this way).
    // Anything else transparent is treated as not visible.
    const iconInput = tag === 'input' &&
      (el.type === 'checkbox' || el.type === 'radio') && target === el;
    const transparentOk = iconInput ||
      ((tag === 'select' || tag === 'input') && target === el);
    if (parseFloat(ts.opacity) < 0.05 && !transparentOk) continue;
    // a multi-line inline text link is boxed on its FIRST line; a link that
    // wraps an image is boxed on the UNION with that image, because an inline
    // anchor's own rect covers only the line box, not the picture
    const lineRects = Array.from(target.getClientRects())
      .filter(rc => rc.width > 1 && rc.height > 1);
    const imgEl = target.querySelector ? target.querySelector('img, picture, video') : null;
    let r = target.getBoundingClientRect();
    if (!imgEl && lineRects.length > 1 && lineRects[0].height < 40) r = lineRects[0];
    if (imgEl) {
      const ir = imgEl.getBoundingClientRect();
      if (ir.width > 1 && ir.height > 1) {
        const left = Math.min(r.left, ir.left), top = Math.min(r.top, ir.top);
        const right = Math.max(r.right, ir.right), bottom = Math.max(r.bottom, ir.bottom);
        r = {left, top, right, bottom, width: right - left, height: bottom - top};
      }
    }
    const minSide = tag === 'iframe' ? 40 : 8;
    // some real links report a zero-size rect while their positioned children
    // carry the whole click surface (the MDN top banner does this): fall back
    // to the bounding rect of the element's contents
    if ((r.width < minSide || r.height < minSide) && target.childNodes && target.childNodes.length) {
      try {
        const rng = document.createRange();
        rng.selectNodeContents(target);
        const cr = rng.getBoundingClientRect();
        if (cr.width >= minSide && cr.height >= minSide) r = cr;
      } catch (e) {}
    }
    if (r.width < minSide || r.height < minSide) continue;
    const ix = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const iy = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    if (ix < minSide || iy < minSide) continue;
    if (ix * iy < 0.5 * r.width * r.height) continue;
    // hit-test a spread of points inside the visible box: a candidate that
    // never comes back from elementFromPoint is painted over or invisible
    // (hidden skip links under the header, say) and gets no box
    const x1 = Math.max(0, r.left), x2 = Math.min(vw, r.right);
    const y1 = Math.max(0, r.top), y2 = Math.min(vh, r.bottom);
    let seen = false;
    for (const [fx, fy] of [[.5, .5], [.25, .25], [.75, .25], [.25, .75],
                            [.75, .75], [.5, .25], [.5, .75]]) {
      const hit = deepHit(x1 + (x2 - x1) * fx, y1 + (y2 - y1) * fy);
      if (hit && (composedContains(target, hit) ||
                  (hit.contains && hit.contains(target)))) { seen = true; break; }
    }
    if (!seen) continue;
    const box = {left: Math.max(0, r.left), top: Math.max(0, r.top),
                 right: Math.min(vw, r.right), bottom: Math.min(vh, r.bottom)};
    box.width = box.right - box.left; box.height = box.bottom - box.top;
    cand.push({el: target, orig: el, rect: box});
  }

  // drop card-nested children: a candidate inside a card-sized interactive
  // container (by DOM containment OR >=90% geometric containment) is dropped so
  // the outer clickable card is the single boxed element
  const vpArea = vw * vh;
  const area = (c) => c.rect.width * c.rect.height;
  const isCard = (c) => {
    const t = c.el.tagName.toLowerCase();
    const role = c.orig.getAttribute('role') || '';
    return (t === 'a' || role === 'button' || role === 'link') &&
      area(c) > 15000 && area(c) < 0.3 * vpArea;
  };
  const geomInside = (inner, outer) => {
    const a = inner.rect, b = outer.rect;
    const gx = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
    const gy = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
    return (gx * gy) / (a.width * a.height || 1) > 0.9;
  };
  const nested = new Set();
  for (const inner of cand) {
    let p = composedParent(inner.el), domNested = false;
    while (p) { if (cand.some(c => c.el === p)) { domNested = true; break; } p = composedParent(p); }
    if (domNested) { nested.add(inner); continue; }
    for (const outer of cand) {
      if (outer === inner || !isCard(outer)) continue;
      if (area(inner) >= 0.5 * area(outer)) continue;
      if ((outer.el.contains && outer.el.contains(inner.el)) || geomInside(inner, outer)) {
        nested.add(inner); break;
      }
    }
  }
  const flat = cand.filter(c => !nested.has(c));

  // drop near-duplicate boxes (same/overlapping rect), keep the first occurrence
  const kept = [];
  for (const c of flat) {
    let dup = false;
    for (const k of kept) {
      const a = c.rect, b = k.rect;
      const gx = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
      const gy = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
      const inter = gx * gy, a1 = a.width * a.height, a2 = b.width * b.height;
      if (inter / Math.min(a1, a2 || 1) > 0.85 &&
          Math.min(a1, a2) / Math.max(a1, a2 || 1) > 0.6) { dup = true; break; }
    }
    if (!dup) kept.push(c);
  }

  const out = kept.map(c => {
    const r = c.rect;
    const img = c.el.querySelector ? c.el.querySelector('img[alt]') : null;
    let labText = '';
    if (c.orig.labels && c.orig.labels.length) labText = (c.orig.labels[0].innerText || '').trim();
    const aria = c.orig.getAttribute('aria-label') || '';
    const name = (aria || c.orig.title || c.orig.placeholder ||
                  (c.el.innerText || '').trim().slice(0, 80) ||
                  (img ? img.alt : '') || labText || c.orig.value || c.orig.name ||
                  c.orig.id || '').trim();
    return {
      tag: c.orig.tagName.toLowerCase(),
      role: c.orig.getAttribute('role') || '',
      type: c.orig.getAttribute('type') || '',
      aria: aria,
      name: name,
      text: (c.el.innerText || '').trim().slice(0, 80),
      href: c.orig.href || '',
      in_shadow: c.orig.getRootNode() instanceof ShadowRoot,
      boxed_via_label: c.el !== c.orig,
      box_css: [Math.max(0, r.left), Math.max(0, r.top),
                Math.min(vw, r.right), Math.min(vh, r.bottom)]
    };
  });
  // reading order: rows of 24 css px, top to bottom then left to right
  out.sort((a, b) => {
    const ra = Math.round(a.box_css[1] / 24), rb = Math.round(b.box_css[1] / 24);
    return ra !== rb ? ra - rb : a.box_css[0] - b.box_css[0];
  });
  return out;
}
"""


def draft_functionality(el):
    """Mechanical, model-free BEHAVIOUR description from a captured element's DOM
    facts (ported from the reference ``draft_functionality``). Grades the ACTION
    an element performs — e.g. "Opens the German Wikipedia" — not its nominal
    name, so the answer key rewards understanding what the control DOES."""
    tag = (el.get("tag") or "").lower()
    href = el.get("href") or ""
    etype = (el.get("type") or "").lower()
    name = (el.get("name") or el.get("text") or tag or "").strip()
    if tag == "a" and href:
        return "Opens " + (name or href)
    if (tag in ("input", "textarea")
            and etype in ("text", "search", "email", "")
            and not el.get("boxed_via_label")):
        return "Focuses the field to type: " + name
    if tag == "select":
        return "Opens the option list: " + name
    if tag == "summary":
        return "Expands or collapses: " + name
    if el.get("boxed_via_label"):
        return "Toggles: " + name
    return "Activates " + name


def _boxes_to_detections(boxes, width, height, dsf):
    """Coordinate adapter: DOM ``box_css`` rects are ABSOLUTE CSS pixels, so
    multiply by the device scale factor to reach screenshot pixels, then map
    into Gemini's 0-1000 normalized [ymin,xmin,ymax,xmax] space against the real
    screenshot ``width``/``height``. This lets us reuse imaging.annotate_image
    unchanged (which expects 0-1000 boxes) — we do NOT reuse the Gemini scaling
    on raw pixels. label/description carry the element name + drafted behaviour."""
    dets = []
    for b in boxes:
        box = b.get("box_css")
        if not (isinstance(box, (list, tuple)) and len(box) == 4):
            continue
        left, top, right, bottom = (float(v) * dsf for v in box)
        w = float(width) or 1.0
        h = float(height) or 1.0
        box_2d = [
            round(top / h * 1000),
            round(left / w * 1000),
            round(bottom / h * 1000),
            round(right / w * 1000),
        ]
        name = (b.get("name") or b.get("text") or b.get("tag") or "").strip()
        dets.append({
            "box_2d": box_2d,
            "label": name[:60],
            "description": draft_functionality(b),
        })
    return dets


def apply_omit(boxes, omit):
    """Drop ONE interactive element matching the ``omit`` directive from the
    drawn boxes and record it as the deliberately-omitted ground-truth element,
    so the labeling question's coverage gate answer is "No" by construction — an
    interactive element is present on the page but carries no box.

    ``omit`` is ``{"match_tag","match_type","match_text"}`` (any subset); a box
    matches when its tag equals match_tag (when given), its type equals
    match_type (when given), and match_text is a substring of its text/name (when
    given). Returns ``(kept_boxes, omitted_record_or_None)``; a no-op (returns all
    boxes, None) when omit is empty or nothing matches, mirroring the reference
    generator which also records a failure when the planned element is absent."""
    if not isinstance(omit, dict) or not omit:
        return list(boxes), None
    mtag = (omit.get("match_tag") or "").strip().lower()
    mtype = (omit.get("match_type") or "").strip().lower()
    mtext = (omit.get("match_text") or "").strip().lower()
    for i, b in enumerate(boxes):
        if mtag and (b.get("tag") or "").lower() != mtag:
            continue
        if mtype and (b.get("type") or "").lower() != mtype:
            continue
        if mtext and mtext not in (
                (b.get("text") or "") + " " + (b.get("name") or "")).lower():
            continue
        omitted = {
            "tag": b.get("tag") or "",
            "type": b.get("type") or "",
            "role": b.get("role") or "",
            "aria": b.get("aria") or "",
            "text": b.get("text") or "",
            "name": b.get("name") or "",
            "href": b.get("href") or "",
            "box_css": list(b.get("box_css") or []),
            "reason": "deliberately left unboxed so the coverage answer is No",
        }
        kept = [x for j, x in enumerate(boxes) if j != i]
        return kept, omitted
    return list(boxes), None


def capture_and_annotate(url, viewport=(1440, 900), dsf=2, omit=None,
                         dismiss=None, wait_ms=2500):
    """Open ``url`` in headless Chromium, enumerate its interactive DOM elements,
    screenshot it, draw numbered boxes at the real DOM geometry (via the shared
    imaging overlay), and mechanically draft a behavioural answer key.

    ``wait_ms`` is the settle delay after ``load`` (default 2500) before the DOM
    is enumerated; ``dismiss`` is a list of CSS selectors for cookie/consent
    "accept" controls — each is clicked (errors ignored, exactly as the reference
    generator's dismiss loop) so an overlay does not hide the real page elements.

    When ``omit`` is set, ONE matching interactive element is left unboxed and
    recorded as the ground-truth omission so ``coverage_expected`` is "no" (the
    coverage gate is answerably "No" by construction); otherwise coverage_expected
    is "yes". Returns ``{"screenshot_png": bytes, "annotated_png": bytes,
    "dom_manifest": [...], "behavioural_key": [...], "label_key": [...],
    "omitted_element": {...}|None, "coverage_expected": "yes"|"no"}`` where
    dom_manifest carries the per-box construction facts, behavioural_key the
    graded ACTION per box, and label_key the geometry answer sheet (box_px) the
    portal renders. Raises RuntimeError when Playwright is unavailable."""
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright is not available; cannot capture a live URL.")
    from . import imaging
    vw, vh = viewport
    try:
        settle = int(wait_ms)
    except (TypeError, ValueError):
        settle = 2500
    if settle < 0:
        settle = 2500
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                viewport={"width": int(vw), "height": int(vh)},
                device_scale_factor=dsf)
            page.goto(url, wait_until="load", timeout=60000)
            page.wait_for_timeout(settle)
            # dismiss cookie/consent overlays: click each selector, ignore any
            # error (the overlay may be absent), then settle briefly so the
            # overlay finishes tearing down before the DOM is enumerated
            dismissed = False
            for sel in (dismiss or []):
                if not str(sel).strip():
                    continue
                try:
                    page.click(sel, timeout=2000)
                    dismissed = True
                except Exception:  # noqa: BLE001 - overlay may not be present
                    pass
            if dismissed:
                page.wait_for_timeout(500)
            boxes = page.evaluate(_COLLECT_JS) or []
            screenshot_png = page.screenshot(full_page=False)
        finally:
            browser.close()
    boxes, omitted = apply_omit(boxes, omit)
    from PIL import Image
    width, height = Image.open(io.BytesIO(screenshot_png)).size
    dets = _boxes_to_detections(boxes, width, height, dsf)
    annotated_png, label_key = imaging.annotate_image(screenshot_png, dets)
    dom_manifest, behavioural_key = _build_manifest_and_key(boxes)
    return {
        "screenshot_png": screenshot_png,
        "annotated_png": annotated_png,
        "dom_manifest": dom_manifest,
        "behavioural_key": behavioural_key,
        "label_key": label_key,
        "omitted_element": omitted,
        "coverage_expected": "no" if omitted else "yes",
    }


def _build_manifest_and_key(boxes):
    """Split the captured elements into the construction manifest (facts) and the
    behavioural answer key (graded action), numbered 1..N in reading order to
    match the numbered boxes on the annotated overlay."""
    dom_manifest = []
    behavioural_key = []
    n = 0
    for b in boxes:
        box = b.get("box_css")
        if not (isinstance(box, (list, tuple)) and len(box) == 4):
            continue
        n += 1
        name = (b.get("name") or b.get("text") or b.get("tag") or "").strip()
        dom_manifest.append({
            "number": n,
            "tag": b.get("tag") or "",
            "role": b.get("role") or "",
            "name": name,
            "text": (b.get("text") or "").strip(),
            "href": b.get("href") or "",
            "box_css": list(box),
            "in_shadow": bool(b.get("in_shadow")),
            "boxed_via_label": bool(b.get("boxed_via_label")),
        })
        behavioural_key.append({
            "number": n,
            "element": (name[:60] or (b.get("tag") or "")),
            "functionality": draft_functionality(b),
        })
    return dom_manifest, behavioural_key


def viewport_stamp(viewport=(1600, 1000), dsf=2):
    """A human-readable capture descriptor + UTC timestamp for staleness, e.g.
    ``1600x1000@2x 2026-07-13T10:20:30Z``."""
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return "%dx%d@%dx %s" % (int(viewport[0]), int(viewport[1]), int(dsf), ts)
