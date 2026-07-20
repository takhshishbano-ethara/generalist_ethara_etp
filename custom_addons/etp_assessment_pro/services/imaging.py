# -*- coding: utf-8 -*-
"""Numbered-box overlay for image_label detection, using Pillow (Odoo core).

Mirrors the reference proj-2 asset generator: red bounding rectangles + numbered
badges placed with a 7-candidate overlap-avoiding search (so a badge never sits
over a neighbouring element or another number), and a label_key answer sheet.
"""
import io

_RED = (214, 40, 40)
_WHITE = (255, 255, 255)


def _harden_pillow():
    """M-3: bound Pillow's decompression-bomb exposure. A crafted image with a
    huge declared canvas (CVE-2023-4863 libwebp class + generic DoS) can pin RAM
    when opened. Cap the pixel count so PIL raises DecompressionBombError instead
    of allocating gigabytes. 64MP is far above any real question stimulus."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 64_000_000
    except Exception:  # noqa: BLE001 - never break rendering if PIL is odd
        pass


_harden_pillow()


def _to_pixels(box_2d, w, h):
    ymin, xmin, ymax, xmax = box_2d  # Gemini order [ymin,xmin,ymax,xmax], 0-1000
    return (
        round(xmin / 1000 * w),
        round(ymin / 1000 * h),
        round(xmax / 1000 * w),
        round(ymax / 1000 * h),
    )


def _font(size):
    from PIL import ImageFont
    for name in ("Arial Bold.ttf", "Arial.ttf", "DejaVuSans-Bold.ttf",
                 "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rects_intersect(a, b):
    """True when two [l,t,r,b] rects overlap (touching edges do not count)."""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _intersect_area(a, b):
    """Overlap area of two [l,t,r,b] rects (0 when disjoint)."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def place_badge(box, bw, bh, boxes, placed, w, h, own_idx):
    """Choose the least-overlapping badge rect for element ``box`` ([l,t,r,b]).

    Ported from the reference ``place_badge``: try seven candidate anchors around
    the element (above-left, left, right, above-right, below, inside-top-left,
    inside-top-right), skip any that fall off-image or collide with an
    already-``placed`` badge, and score the rest by how much they overlap OTHER
    element ``boxes`` (never the element's own box at ``own_idx``); take the first
    zero-overlap candidate, else the least-overlapping one. Falls back to just
    inside the top-left corner (overlap -1) when every candidate is blocked.
    Returns ``(rect, overlap_px)`` with rect as [l,t,r,b]."""
    left, top, right, bottom = box
    candidates = [
        (left, top - bh - 2),           # above, flush left, outside the box
        (left - bw - 2, top),           # left of the box
        (right + 2, top),               # right of the box
        (right - bw, top - bh - 2),     # above, flush right
        (left + 2, bottom + 2),         # below the box
        (left + 2, top + 2),            # inside the top-left corner
        (right - bw - 2, top + 2),      # inside the top-right corner
    ]
    scored = []
    for cx, cy in candidates:
        if cx < 0 or cy < 0 or cx + bw > w or cy + bh > h:
            continue
        rect = (cx, cy, cx + bw, cy + bh)
        if any(_rects_intersect(rect, p) for p in placed):
            continue
        overlap = 0
        for i, ob in enumerate(boxes):
            if i == own_idx:
                continue
            overlap += _intersect_area(rect, ob)
        scored.append((overlap, rect))
        if overlap == 0:
            break
    if not scored:
        rect = (left + 2, top + 2, left + 2 + bw, top + 2 + bh)
        return rect, -1
    scored.sort(key=lambda t: t[0])
    return scored[0][1], scored[0][0]


def annotate_from_pixels(image_bytes, items):
    """Numbered-box overlay working directly in IMAGE-PIXEL space.

    ``items`` is ``[{"label","description","box_px":[l,t,r,b]}]`` with box_px
    already in the image's own pixel coordinates — NO 0-1000 round trip — so a
    caller holding exact rects (the DOM capture path, whose boxes are CSS px *
    device-scale-factor) draws them tight on the real element edges instead of
    quantizing every edge to the ~1.4px steps of Gemini's 0-1000 grid. Element
    rectangles are drawn first, then each numbered badge is positioned with
    :func:`place_badge` so it avoids covering neighbouring boxes and other badges.
    Returns ``(annotated_png_bytes, label_key)`` where label_key is the answer
    sheet ``[{"number","label","description","box_px":[l,t,r,b]}]``."""
    from PIL import Image, ImageDraw
    base = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = base.size
    draw = ImageDraw.Draw(base)
    fsize = max(13, w // 90)
    font = _font(fsize)

    valid = [it for it in (items or [])
             if isinstance(it, dict)
             and isinstance(it.get("box_px"), (list, tuple))
             and len(it["box_px"]) == 4]
    px_boxes = [[int(round(float(v))) for v in it["box_px"]] for it in valid]

    for left, top, right, bottom in px_boxes:
        draw.rounded_rectangle(
            [left, top, right, bottom], radius=6, outline=_RED, width=2)

    label_key = []
    placed = []
    for i, it in enumerate(valid):
        number = str(i + 1)
        tb = draw.textbbox((0, 0), number, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        bw, bh = tw + 10, th + 8
        rect, _overlap = place_badge(
            px_boxes[i], bw, bh, px_boxes, placed, w, h, i)
        placed.append(rect)
        draw.rectangle([rect[0], rect[1], rect[2], rect[3]], fill=_RED)
        draw.text(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2),
                  number, fill=_WHITE, font=font, anchor="mm")
        left, top, right, bottom = px_boxes[i]
        label_key.append({
            "number": i + 1,
            "label": str(it.get("label") or "").strip(),
            "description": str(it.get("description") or "").strip(),
            "box_px": [left, top, right, bottom],
        })

    buf = io.BytesIO()
    base.save(buf, format="PNG")
    return buf.getvalue(), label_key


def annotate_image(image_bytes, detections):
    """Draw numbered red boxes for ``detections`` (each ``{"box_2d","label",
    "description"}`` with box_2d in 0-1000 space) onto ``image_bytes``. Element
    rectangles are drawn first, then each numbered badge is positioned with
    :func:`place_badge` so it avoids covering neighbouring boxes and other badges.
    Returns ``(annotated_png_bytes, label_key)`` where label_key is the answer
    sheet ``[{"number","label","description","box_px":[l,t,r,b]}]``.

    This is the Gemini-detection entry point: box_2d rects are mapped from the
    0-1000 grid into pixel space against the image size, then drawn by
    :func:`annotate_from_pixels`. Callers that already hold exact pixel rects
    (the DOM capture path) should call :func:`annotate_from_pixels` directly."""
    from PIL import Image
    w, h = Image.open(io.BytesIO(image_bytes)).size
    items = []
    for d in (detections or []):
        if not (isinstance(d, dict)
                and isinstance(d.get("box_2d"), (list, tuple))
                and len(d["box_2d"]) == 4):
            continue
        items.append({
            "label": d.get("label"),
            "description": d.get("description"),
            "box_px": list(_to_pixels(d["box_2d"], w, h)),
        })
    return annotate_from_pixels(image_bytes, items)
