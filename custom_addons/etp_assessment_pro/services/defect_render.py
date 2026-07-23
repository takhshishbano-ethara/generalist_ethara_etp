# -*- coding: utf-8 -*-
"""True-by-construction defect renderer for single-image `image_label` defect
annotation (the q7r task family), ported from the research harness
`renderers/defects.py` and hardened for our pipeline.

WHY THIS EXISTS
---------------
An assessment answer key must be PROVABLE: the defect at a *known* pixel. A model
that "adds a defect somewhere" gives an unknown location, so the key becomes a
guess and the numbered marker lands on empty space (exactly the bad q7r labels).
So the model decides *what/where* (the spec); this code *places* the defect
deterministically with PIL, and stamps the numbered marker on it.

IMPROVEMENT OVER THE RESEARCH RENDERER
--------------------------------------
Research stamps the marker at the model-authored ``marker_xy`` and lets
region-modifying ops (float_copy / smear / shadow / warp / smooth) act on a blind
pixel box the model guessed without seeing the rendered base — so the modification
and its marker can both land on empty table (the misplaced-label bug we saw on
q7r). Here every op RETURNS THE ACTUAL BOUNDING BOX IT DREW INTO, and the marker
is stamped at that box's true center. If an op cannot run (bad/way-off box), it is
dropped from the answer key rather than shipping a marker over nothing. The marker
always sits on a real, planted defect.

Everything here is deterministic PIL — no model calls. Coordinates are in a fixed
1280x720 canvas; the base image is resized to it first.

    plant(base_png_bytes, defects) -> (original_png, annotated_png, planted[])
"""
import io
import math
import os
import random

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


def plant(base_png_bytes, defects, seed=7):
    """Resize the base image to the canvas, plant every defect deterministically,
    and return (original_png_bytes, annotated_png_bytes, planted).

    ``planted`` is the answer key the marker is TRUE for:
      [{"marker": n, "op": ..., "marker_xy": [cx, cy], "flaw": "..."}]
    A defect whose op could not run (bad box) is dropped from the key AND gets no
    marker — the marker never sits on empty space.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(base_png_bytes)).convert("RGBA").resize(
        _CANVAS, Image.LANCZOS)
    w, h = img.size
    rng = random.Random(seed)

    planted = []
    for de in (defects or []):
        if not isinstance(de, dict):
            continue
        op = de.get("op")
        fn = _OPS.get(op)
        if fn is None:
            continue
        spec = dict(de.get("spec") or {})
        spec.setdefault("marker_xy", de.get("marker_xy"))
        try:
            drawn_box = fn(img, spec, rng)
        except Exception:  # noqa: BLE001 - a bad box drops the defect, never crashes
            drawn_box = None
        if not (isinstance(drawn_box, (list, tuple)) and len(drawn_box) == 4):
            continue
        bx0, by0, bx1, by1 = drawn_box
        # A degenerate or off-canvas box means the op effectively drew nothing
        # (PIL clamps/returns empty regions instead of raising) - drop it so a
        # marker never lands on empty space, and require a real overlap with the
        # canvas so out-of-bounds specs cannot produce a phantom marker.
        if (bx1 - bx0) < 4 or (by1 - by0) < 4:
            continue
        if bx1 <= 0 or by1 <= 0 or bx0 >= w or by0 >= h:
            continue
        # Marker center = the TRUE center of what the op actually drew. This is the
        # fix for research's misplaced q7r markers (which trusted a blind marker_xy).
        cx, cy = _clamp_center(drawn_box, w, h)
        planted.append({
            "marker": de.get("marker"),
            "op": op,
            "marker_xy": [cx, cy],
            "flaw": de.get("flaw"),
        })

    original = img.convert("RGB")
    ann = img.copy()
    for i, p in enumerate(planted, 1):
        # renumber 1..N in plant order so the key and the overlay always agree,
        # even when some defects were dropped.
        p["marker"] = i
        _draw_marker(ann, i, p["marker_xy"][0], p["marker_xy"][1])
    annotated = ann.convert("RGB")

    obuf, abuf = io.BytesIO(), io.BytesIO()
    original.save(obuf, format="PNG")
    annotated.save(abuf, format="PNG")
    return obuf.getvalue(), abuf.getvalue(), planted
