# -*- coding: utf-8 -*-
"""True-by-construction defect renderer for single-image `image_label` defect
annotation (the q7r task family), ported 1:1 from the research harness
`renderers/defects.py` (drop 2 — the base+edit+vision-ground rework).

WHY THIS EXISTS
---------------
An assessment answer key must be PROVABLE: the numbered marker must sit on a real,
nameable defect, and the key must never reference a defect that didn't render.

THE FLOW (drop 2)
-----------------
1. Render a CLEAN base image from the model's base_prompt (done by the caller).
2. ONE combined image-to-image EDIT bakes every structural defect (fused/extra
   fingers, garbled inline text on the scene's own surfaces, impossible joints,
   objects merging with no clean boundary) into the real pixels — things PIL cannot
   synthesize. The edit re-renders the whole scene, so a pre-guessed pixel is
   worthless.
3. Deterministic PIL is kept ONLY for a genuine clone/duplication artifact
   (`float_copy`) where copying pixels is more reliable than an edit.
4. VISION-GROUND each edit defect's `anchor` (the visible object it sits on) in the
   FINAL image and ask the same call whether the flaw is actually present. The
   marker is stamped at the located box center; a defect the vision call cannot
   confirm present is DROPPED (never shipped as a phantom marker).
5. RENUMBER the surviving markers 1..N (continuous, no gaps) and return a
   `marker_map` so the caller can remap the answer key to match.

The model decides WHAT defect and WHERE (the anchor); vision on the final pixels
decides the exact marker position and whether it made it in. The PIL ops below are
the fallback lane; the vision grounding is the primary path.

    plant(base_png_bytes, defects, ctx) -> (original_png, annotated_png, info)

``ctx`` supplies ``edit_image(prompt, ref_bytes)`` and
``locate(image_bytes, anchor, flaw)`` so this module stays model-agnostic. In our
Odoo wiring those are thin wrappers over services/vertex.py `edit_image` /
`locate_defect`. Coordinates are in a fixed 1280x720 canvas.
"""
import io
import math
import os
import random
import logging

_logger = logging.getLogger(__name__)

_CANVAS = (1280, 720)
_RED = (225, 30, 35, 255)
_WHITE = (255, 255, 255, 255)

_FONT_DIRS = ["/System/Library/Fonts/Supplemental", "/System/Library/Fonts",
              "/Library/Fonts", "/usr/share/fonts", "/usr/share/fonts/truetype",
              "/usr/share/fonts/truetype/dejavu"]

_GARBLE_CHARS = "abcdefghijklmnopqrstuvwxyzftlrneus"


def _harden_pillow():
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 64_000_000
    except Exception:  # noqa: BLE001
        pass


_harden_pillow()


def _font(names, size):
    from PIL import ImageFont
    for d in _FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:  # noqa: BLE001
                    pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _f_sans(size, bold=False):
    return _font(["Arial Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
                 else ["Arial.ttf", "Helvetica.ttc", "DejaVuSans.ttf"], size)


def _f_hand(size):
    return _font(["Comic Sans MS.ttf", "Bradley Hand Bold.ttf", "Marker Felt.ttc",
                  "DejaVuSans.ttf"], size)


def _f_chalk(size):
    return _font(["Chalkduster.ttf", "Comic Sans MS.ttf", "DejaVuSans.ttf"], size)


def _draw_garbled_line(draw, x, y, width, fnt, fill, rng, density=1.0):
    cx = x
    while cx < x + width:
        n = rng.randint(1, 3)
        ch = "".join(rng.choice(_GARBLE_CHARS) for _ in range(n))
        jy = rng.randint(-3, 3)
        draw.text((cx, y + jy), ch, font=fnt, fill=fill)
        try:
            w = draw.textlength(ch, font=fnt)
        except Exception:  # noqa: BLE001
            w = 10 * n
        cx += max(4, int(w * rng.uniform(0.35, 0.75) * density))


def _rounded_card(size, color, border, radius=6):
    from PIL import Image, ImageDraw
    card = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius,
                        fill=color, outline=border, width=2)
    return card


def _paste_rotated(img, card, xy, angle, shadow=True):
    from PIL import Image, ImageFilter
    if angle:
        card = card.rotate(angle, expand=True, resample=Image.BICUBIC)
    if shadow:
        black = Image.new("RGBA", card.size, (20, 15, 5, 255))
        black.putalpha(card.split()[3].point(lambda a: int(a * 0.45)))
        img.alpha_composite(black.filter(ImageFilter.GaussianBlur(4)),
                            (xy[0] + 6, xy[1] + 7))
    img.alpha_composite(card, xy)


# ---- injection ops: each MUTATES img in place and RETURNS the [l,t,r,b] box it
#      actually drew into (in canvas pixels), so the marker can be centered on the
#      real defect rather than a blind guess. Return None to signal "did nothing".

def _op_garbled_card(img, s, rng):
    from PIL import ImageDraw
    w, h = int(s["w"]), int(s["h"])
    x, y = int(s["x"]), int(s["y"])
    card = _rounded_card((w, h), tuple(s.get("color", [244, 238, 220, 255])),
                         (120, 110, 90, 255))
    d = ImageDraw.Draw(card)
    fnt = _f_sans(s.get("font_size", 22))
    top = s.get("top_pad", 14)
    if s.get("title"):
        d.text((14, top), s["title"], font=_f_sans(s.get("title_size", 26), bold=True),
               fill=(60, 50, 30, 255))
        top += s.get("title_size", 26) + 10
    lines = s.get("lines", 4)
    line_h = (h - top - 12) // max(1, lines)
    for i in range(lines):
        _draw_garbled_line(d, 14, top + i * line_h, w - 28, fnt,
                           tuple(s.get("ink", [55, 45, 30, 255])), rng)
    _paste_rotated(img, card, (x, y), s.get("angle", 0), s.get("shadow", True))
    return [x, y, x + w, y + h]


def _op_text_card(img, s, rng):
    from PIL import ImageDraw
    w, h = int(s["w"]), int(s["h"])
    x, y = int(s["x"]), int(s["y"])
    card = _rounded_card((w, h), tuple(s.get("color", [40, 36, 32, 255])),
                         tuple(s.get("border", [200, 195, 185, 255])))
    d = ImageDraw.Draw(card)
    top = s.get("top_pad", 12)
    for ln in s.get("text_lines", []):
        size = int(ln.get("size", 24))
        fnt = {"hand": _f_hand, "chalk": _f_chalk}.get(
            ln.get("font", "sans"), lambda z: _f_sans(z, ln.get("bold", False)))(size)
        ink = tuple(ln.get("ink", [235, 230, 220, 255]))
        if ln.get("garbled"):
            _draw_garbled_line(d, ln.get("x", 14), top, w - ln.get("x", 14) - 14,
                               fnt, ink, rng)
        else:
            d.text((ln.get("x", 14), top), ln.get("text", ""), font=fnt, fill=ink)
        top += size + ln.get("gap", 8)
    _paste_rotated(img, card, (x, y), s.get("angle", 0), s.get("shadow", True))
    return [x, y, x + w, y + h]


def _op_garbled_words(img, s, rng):
    from PIL import ImageDraw
    fnt = {"hand": _f_hand, "chalk": _f_chalk}.get(
        s.get("font", "sans"), lambda z: _f_sans(z, s.get("bold", False)))(s.get("size", 28))
    d = ImageDraw.Draw(img)
    mxy = s.get("marker_xy") or [s.get("x", 0), s.get("y", 0)]
    x = int(s.get("x", mxy[0]))
    y = int(s.get("y", mxy[1]))
    w = int(s.get("w", 240))
    lines = s.get("lines", 1)
    yy = y
    for _ in range(lines):
        _draw_garbled_line(d, x, yy, w, fnt, tuple(s.get("ink", [40, 35, 30, 255])), rng)
        yy += s.get("size", 28) + s.get("gap", 8)
    return [x, y, x + w, yy]


def _op_float_copy(img, s, rng):
    from PIL import Image, ImageDraw, ImageFilter
    box = [int(v) for v in s["src_box"]]
    region = img.crop(box)
    if s.get("scale", 1.0) != 1.0:
        region = region.resize(
            (int(region.width * s["scale"]), int(region.height * s["scale"])),
            Image.LANCZOS)
    mask = Image.new("L", region.size, 0)
    md = ImageDraw.Draw(mask)
    if s.get("mask", "rounded") == "ellipse":
        md.ellipse([2, 2, region.width - 3, region.height - 3], fill=255)
    else:
        md.rounded_rectangle([2, 2, region.width - 3, region.height - 3],
                             radius=min(18, region.width // 4), fill=255)
    dx, dy = int(s["dst_x"]), int(s["dst_y"])
    img.paste(region, (dx, dy), mask.filter(ImageFilter.GaussianBlur(3)))
    return [dx, dy, dx + region.width, dy + region.height]


def _op_smear(img, s, rng):
    from PIL import Image, ImageDraw, ImageFilter
    x0, y0, x1, y1 = [int(v) for v in s["box"]]
    dx, dy = s.get("dx", 6), s.get("dy", 0)
    out = img.crop((x0, y0, x1, y1))
    for i in range(1, s.get("steps", 7)):
        out = Image.blend(
            out, img.crop((x0 - dx * i, y0 - dy * i, x1 - dx * i, y1 - dy * i)), 0.5)
    out = out.filter(ImageFilter.GaussianBlur(s.get("blur", 3)))
    mask = Image.new("L", out.size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, out.width, out.height], fill=255)
    img.paste(out, (x0, y0), mask.filter(ImageFilter.GaussianBlur(8)))
    return [x0, y0, x1, y1]


def _op_warp(img, s, rng):
    from PIL import Image, ImageDraw, ImageFilter
    x0, y0, x1, y1 = [int(v) for v in s["box"]]
    region = img.crop((x0, y0, x1, y1))
    w, h = region.size
    amp, wl = s.get("amp", 10), s.get("wavelength", 40)
    out = Image.new("RGBA", region.size)
    if s.get("axis", "x") == "x":
        for yy in range(h):
            off = int(amp * math.sin(2 * math.pi * yy / wl))
            out.paste(region.crop((0, yy, w, yy + 1)), (off, yy))
    else:
        for xx in range(w):
            off = int(amp * math.sin(2 * math.pi * xx / wl))
            out.paste(region.crop((xx, 0, xx + 1, h)), (xx, off))
    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).rectangle([amp + 2, 2, w - amp - 2, h - 2], fill=255)
    img.paste(out, (x0, y0), mask.filter(ImageFilter.GaussianBlur(6)))
    return [x0, y0, x1, y1]


def _op_smooth(img, s, rng):
    from PIL import Image, ImageDraw, ImageFilter
    x0, y0, x1, y1 = [int(v) for v in s["box"]]
    region = img.crop((x0, y0, x1, y1))
    sm = region.filter(ImageFilter.GaussianBlur(s.get("blur", 9))).filter(
        ImageFilter.MedianFilter(size=5))
    if s.get("lift", 12):
        sm = Image.eval(sm, lambda p: min(255, p + s.get("lift", 12)))
    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, region.width, region.height], fill=255)
    img.paste(sm, (x0, y0), mask.filter(ImageFilter.GaussianBlur(10)))
    return [x0, y0, x1, y1]


def _op_shadow(img, s, rng):
    from PIL import Image, ImageDraw, ImageFilter
    box = [int(v) for v in s["box"]]
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).ellipse(box, fill=(10, 8, 5, s.get("opacity", 130)))
    img.alpha_composite(overlay.filter(ImageFilter.GaussianBlur(s.get("blur", 10))))
    return box


_OPS = {
    "garbled_card": _op_garbled_card, "text_card": _op_text_card,
    "garbled_words": _op_garbled_words, "float_copy": _op_float_copy,
    "smear": _op_smear, "warp": _op_warp, "smooth": _op_smooth,
    "shadow": _op_shadow,
}


def _draw_marker(img, n, cx, cy):
    """Stamp a numbered red target marker centered at (cx, cy)."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    r = 20
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=_RED, width=4)
    d.line([cx - 9, cy, cx + 9, cy], fill=(255, 255, 255, 230), width=3)
    d.line([cx, cy - 9, cx, cy + 9], fill=(255, 255, 255, 230), width=3)
    fnt = _f_sans(26, bold=True)
    tx, ty = cx + r + 3, cy - r - 14
    if tx > img.width - 30:
        tx = cx - r - 26
    if ty < 4:
        ty = cy + r + 2
    for ox, oy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        d.text((tx + ox, ty + oy), str(n), font=fnt, fill=_WHITE)
    d.text((tx, ty), str(n), font=fnt, fill=_RED)


def _clamp_center(box, w, h):
    cx = max(0, min(w - 1, (box[0] + box[2]) // 2))
    cy = max(0, min(h - 1, (box[1] + box[3]) // 2))
    return cx, cy


def _region_center(region):
    if isinstance(region, (list, tuple)) and len(region) == 4:
        x0, y0, x1, y1 = region
        return int((x0 + x1) / 2), int((y0 + y1) / 2)
    return _CANVAS[0] // 2, _CANVAS[1] // 2


def _box_center_px(box_2d):
    """[ymin,xmin,ymax,xmax] on a 0-1000 grid -> (x,y) clamped to the canvas."""
    ymin, xmin, ymax, xmax = box_2d
    cx = int((xmin + xmax) / 2 / 1000 * _CANVAS[0])
    cy = int((ymin + ymax) / 2 / 1000 * _CANVAS[1])
    return max(0, min(_CANVAS[0] - 1, cx)), max(0, min(_CANVAS[1] - 1, cy))


def _combined_brief(edit_defects):
    """One edit call for every structural defect, so the scene re-renders ONCE
    (less drift, fewer unplanted artifacts) instead of once per defect. Ported 1:1
    from the research harness `_combined_brief`."""
    lines = []
    for i, d in enumerate(edit_defects, 1):
        instr = (d.get("edit_instruction") or d.get("flaw") or "").strip()
        lines.append("%d. %s" % (i, instr))
    return ("Edit this photograph to introduce EXACTLY these localized flaws, and "
            "nothing else:\n" + "\n".join(lines) +
            "\nApply every change. Keep the rest of the scene as close to the "
            "original as possible: same composition, same subject and pose, same "
            "lighting and colors. Do not add any other defects or text. Return the "
            "edited photograph.")


def _is_edit(d):
    """A defect is applied by generative EDIT (vs deterministic PIL) unless it
    explicitly names a PIL op with no edit instruction. Mirrors research `_is_edit`."""
    method = d.get("method") or (
        "pil" if (d.get("op") in _OPS and not d.get("edit_instruction")) else "edit")
    return method == "edit" and bool(d.get("edit_instruction") or d.get("flaw"))


def _apply_pil(work, d, rng):
    """Deterministic clone/duplication op on the (already edited) image. Returns
    (image, result_record); marker comes from the spec since PIL plants at a known
    pixel. Mirrors research `_apply_pil` (kept ONLY for genuine clone/duplication)."""
    import numpy as np
    rec = {"marker": d.get("marker"), "method": "pil", "op": d.get("op"),
           "kind": d.get("kind"), "flaw": d.get("flaw")}
    before = np.asarray(work.convert("RGB"))
    try:
        spec = dict(d.get("spec", {}) or {})
        spec.setdefault("marker_xy", d.get("marker_xy"))
        fn = _OPS.get(d.get("op"))
        drawn_box = fn(work, spec, rng) if fn else None
        after = np.asarray(work.convert("RGB"))
        cs = float(np.abs(after.astype(np.int16) - before.astype(np.int16)).mean())
        mxy = d.get("marker_xy") or spec.get("marker_xy")
        if (not mxy) and isinstance(drawn_box, (list, tuple)) and len(drawn_box) == 4:
            mxy = list(_clamp_center(drawn_box, work.width, work.height))
        rec.update(marker_xy=mxy, verified=cs > 0.5, change_score=round(cs, 2))
        return work, rec
    except Exception as e:  # noqa: BLE001
        _logger.warning("defect_render pil marker %s op %s failed: %s",
                        d.get("marker"), d.get("op"), e)
        rec.update(marker_xy=d.get("marker_xy"), verified=False, change_score=0.0)
        return work, rec


def plant(base_png_bytes, defects, ctx=None, seed=7):
    """BASE + EDIT + VISION-GROUND defect renderer — ported 1:1 from the research
    harness `renderers/defects.py::render` (drop 2).

    Flow: resize base to canvas -> ONE combined image-to-image edit bakes every
    structural defect into the real pixels -> deterministic PIL for any
    clone/duplication -> vision-ground each edit defect's `anchor` on the FINAL
    image and verify the flaw is present -> stamp markers ONLY on verified defects
    -> renumber survivors 1..N (continuous, no gaps) and return a marker_map so the
    answer key can be remapped to match.

    ``ctx`` supplies the two model callables (kept out of this pure-PIL module so it
    stays model-agnostic and unit-testable):
        ctx.edit_image(edit_prompt, ref_png_bytes) -> edited_png_bytes
        ctx.locate(image_png_bytes, anchor, flaw)  -> {found, box_2d, defect_present}
    When ``ctx`` is None (or lacks a callable) the flow degrades: edit defects fall
    back to the region center unverified, PIL defects still plant deterministically.

    Returns (original_png_bytes, annotated_png_bytes, info) where info carries
    planted_markers (1..N), marker_map (model marker -> new sequential), labels,
    assets_verified, and the per-defect result records.
    """
    from PIL import Image
    work = Image.open(io.BytesIO(base_png_bytes)).convert("RGB").resize(
        _CANVAS, Image.LANCZOS)
    rng = random.Random(seed)
    can_edit = callable(getattr(ctx, "edit_image", None))
    can_locate = callable(getattr(ctx, "locate", None))

    defects = [d for d in (defects or []) if isinstance(d, dict)]
    edit_defs = [d for d in defects if _is_edit(d)]
    pil_defs = [d for d in defects if not _is_edit(d)]

    # 1) one combined edit bakes every structural defect into the pixels at once
    if edit_defs and can_edit:
        try:
            brief = _combined_brief(edit_defs)
            edited = ctx.edit_image(brief, _png_bytes(work))
            work = Image.open(io.BytesIO(edited)).convert("RGB").resize(
                _CANVAS, Image.LANCZOS)
        except Exception as e:  # noqa: BLE001 — a failed edit degrades, never crashes
            _logger.warning("defect_render combined edit failed: %s", e)

    # 2) deterministic PIL clone/duplication ops, applied on the edited image
    work = work.convert("RGBA")
    results = []
    for d in pil_defs:
        work, rec = _apply_pil(work, d, rng)
        results.append(rec)

    # 3) vision-ground each edit defect ON the worker-facing image
    original_rgb = work.convert("RGB")
    original_png = _png_bytes(original_rgb)
    for d in edit_defs:
        anchor = d.get("anchor") or d.get("flaw") or ""
        rec = {"marker": d.get("marker"), "method": "edit", "op": "edit",
               "kind": d.get("kind"), "flaw": d.get("flaw"), "anchor": anchor}
        loc = None
        if can_locate and anchor:
            try:
                loc = ctx.locate(original_png, anchor, d.get("flaw") or "")
            except Exception as e:  # noqa: BLE001
                _logger.warning("defect_render locate marker %s failed: %s",
                                d.get("marker"), e)
        if loc and loc.get("found") and loc.get("box_2d"):
            present = bool(loc.get("defect_present", True))
            rec.update(marker_xy=list(_box_center_px(loc["box_2d"])), located=True,
                       defect_present=present, verified=present,
                       region_fallback=not present)
        else:
            rec.update(marker_xy=list(_region_center(d.get("region"))),
                       located=False, defect_present=False, verified=False,
                       region_fallback=True)
        results.append(rec)

    results.sort(key=lambda r: r.get("marker")
                 if isinstance(r.get("marker"), int) else 999)

    # 4) stamp markers ONLY for defects actually IN the image (verified +
    #    positioned). RENUMBER the survivors 1..N in order so the annotation
    #    sequence is CONTINUOUS — however many made it in. The count is NOT fixed.
    drawn = [r for r in results if r.get("verified")
             and isinstance(r.get("marker_xy"), (list, tuple))
             and len(r["marker_xy"]) == 2]
    marker_map = {}
    ann = work.copy()
    placed = []                          # keep every marker visually distinct: markers
    _MINSEP = 46                         # are r=20, so centers must be >~46px apart or the
    for i, r in enumerate(drawn, 1):     # circles overlap (harness-3 renderers/defects.py).
        if isinstance(r.get("marker"), int):
            marker_map[r["marker"]] = i
        r["marker"] = i
        x, y = int(r["marker_xy"][0]), int(r["marker_xy"][1])
        for _ in range(16):              # nudge away from any too-close neighbour
            near = [(px, py) for px, py in placed
                    if (px - x) ** 2 + (py - y) ** 2 < _MINSEP * _MINSEP]
            if not near:
                break
            ax = sum(p[0] for p in near) / len(near)
            ay = sum(p[1] for p in near) / len(near)
            dx, dy = x - ax, y - ay
            dist = math.hypot(dx, dy)
            if dist < 1:                 # exactly coincident -> pick a fanned angle
                dx, dy, dist = math.cos(i * 1.7), math.sin(i * 1.7), 1.0
            x = int(x + dx / dist * _MINSEP)
            y = int(y + dy / dist * _MINSEP)
            x = max(26, min(_CANVAS[0] - 26, x))
            y = max(26, min(_CANVAS[1] - 26, y))
        placed.append((x, y))
        r["marker_xy"] = [x, y]          # keep the manifest in sync with the pixel drawn
        _draw_marker(ann, i, x, y)

    original = original_rgb
    annotated = ann.convert("RGB")
    obuf, abuf = io.BytesIO(), io.BytesIO()
    original.save(obuf, format="PNG")
    annotated.save(abuf, format="PNG")

    info = {
        "planted_markers": [r["marker"] for r in drawn],
        "marker_map": marker_map,
        "labels": {str(r["marker"]): r.get("flaw") for r in drawn},
        "assets_verified": len(drawn) == len(results),
        "defects": drawn,
    }
    return obuf.getvalue(), abuf.getvalue(), info


def _png_bytes(pil_img):
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
