/**
 * Backend image-zoom lightbox.
 *
 * Mirrors the candidate portal's image viewer (views/portal_templates.xml,
 * template portal_image_zoom) so an admin reviewing a Draft Question in the
 * backend gets the SAME zoom/pan/preview experience the candidate gets on the
 * assessment screen: click a thumbnail to open a fullscreen overlay, wheel or
 * +/- to zoom (cursor-anchored), drag to pan, double-click to toggle 2.5x,
 * Esc to close.
 *
 * Why a global delegated controller and not an OWL field widget: the draft
 * image thumbnails are emitted as raw HTML by an Html field
 * (prompt.image_preview), so there is no component to hook. A single
 * document-level click listener (event delegation) binds every current and
 * future .etp-image-zoomable node without re-binding on each re-render, and the
 * overlay DOM is created lazily on first use. This file only sets up listeners;
 * it renders nothing until an image is actually clicked.
 */

const MIN = 1;
const MAX = 6;
const STEP = 0.4;

let overlay = null;
let stage = null;
let img = null;
let scale = 1;
let tx = 0;
let ty = 0;
let baseW = 0;
let baseH = 0;
let dragging = false;
let moved = false;
let startX = 0;
let startY = 0;
let startTx = 0;
let startTy = 0;

function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
}

// keep the image from being dragged entirely out of view
function constrain() {
    const maxX = Math.max(0, (baseW * scale - baseW) / 2 + 40);
    const maxY = Math.max(0, (baseH * scale - baseH) / 2 + 40);
    tx = clamp(tx, -maxX, maxX);
    ty = clamp(ty, -maxY, maxY);
}

function apply() {
    constrain();
    img.style.transform =
        "translate(" + tx + "px, " + ty + "px) scale(" + scale + ")";
    overlay.classList.toggle("is-zoomed", scale > 1.001);
}

function reset() {
    scale = 1;
    tx = 0;
    ty = 0;
    apply();
}

// zoom toward a point (cx, cy) measured from the stage centre, keeping that
// point visually fixed (cursor-anchored zoom).
function zoomTo(next, cx, cy) {
    next = clamp(next, MIN, MAX);
    if (!(Math.abs(next - scale) > 0.0001)) {
        return;
    }
    const ratio = next / scale;
    tx = cx - ratio * (cx - tx);
    ty = cy - ratio * (cy - ty);
    scale = next;
    if (!(scale > MIN + 0.0001)) {
        tx = 0;
        ty = 0;
    }
    apply();
}

function stageCenter(e) {
    const r = stage.getBoundingClientRect();
    return {
        x: e.clientX - (r.left + r.width / 2),
        y: e.clientY - (r.top + r.height / 2),
    };
}

function endDrag(e) {
    if (!dragging) {
        return;
    }
    dragging = false;
    stage.classList.remove("is-grabbing");
    try {
        stage.releasePointerCapture(e.pointerId);
    } catch (err) {
        // pointer capture is best-effort
    }
    setTimeout(function () {
        moved = false;
    }, 0);
}

// Build the overlay DOM once, on first open. Kept out of the initial render so
// the backend pays nothing until an admin actually clicks an image.
function buildOverlay() {
    overlay = document.createElement("div");
    overlay.id = "etp-zoom-overlay";
    overlay.className = "etp-zoom-overlay";
    overlay.setAttribute("aria-hidden", "true");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Image viewer");
    overlay.innerHTML =
        '<div class="etp-zoom-toolbar">' +
        '<button type="button" class="etp-zoom-btn" data-etp-zoom-action="out"' +
        ' aria-label="Zoom out" title="Zoom out (-)">' +
        '<i class="fa fa-search-minus"></i></button>' +
        '<button type="button" class="etp-zoom-btn" data-etp-zoom-action="reset"' +
        ' aria-label="Reset zoom" title="Reset zoom (0)">' +
        '<i class="fa fa-refresh"></i></button>' +
        '<button type="button" class="etp-zoom-btn" data-etp-zoom-action="in"' +
        ' aria-label="Zoom in" title="Zoom in (+)">' +
        '<i class="fa fa-search-plus"></i></button>' +
        '<button type="button" class="etp-zoom-btn etp-zoom-close"' +
        ' data-etp-zoom-action="close" aria-label="Close viewer"' +
        ' title="Close (Esc)">\u00d7</button>' +
        "</div>" +
        '<div class="etp-zoom-stage">' +
        '<img class="etp-zoom-img" alt="" draggable="false"' +
        ' oncontextmenu="return false;"/>' +
        "</div>";
    document.body.appendChild(overlay);

    stage = overlay.querySelector(".etp-zoom-stage");
    img = overlay.querySelector(".etp-zoom-img");

    img.addEventListener("load", function () {
        baseW = img.clientWidth || img.naturalWidth || 0;
        baseH = img.clientHeight || img.naturalHeight || 0;
    });

    // toolbar + backdrop clicks
    overlay.addEventListener("click", function (e) {
        const btn = e.target.closest
            ? e.target.closest("[data-etp-zoom-action]")
            : null;
        if (btn) {
            e.preventDefault();
            const a = btn.getAttribute("data-etp-zoom-action");
            if (a === "in") {
                zoomTo(scale + STEP, 0, 0);
            } else if (a === "out") {
                zoomTo(scale - STEP, 0, 0);
            } else if (a === "reset") {
                reset();
            } else if (a === "close") {
                close();
            }
            return;
        }
        if (!moved && (e.target === overlay || e.target === stage)) {
            close();
        }
    });

    // cursor-anchored wheel zoom; preventDefault only while open
    stage.addEventListener(
        "wheel",
        function (e) {
            if (!overlay.classList.contains("is-open")) {
                return;
            }
            e.preventDefault();
            const c = stageCenter(e);
            const dir = e.deltaY > 0 ? -1 : 1;
            zoomTo(scale + dir * STEP, c.x, c.y);
        },
        { passive: false }
    );

    // drag to pan (pointer events cover mouse + touch)
    stage.addEventListener("pointerdown", function (e) {
        if (!(scale > 1.001)) {
            return;
        }
        dragging = true;
        moved = false;
        startX = e.clientX;
        startY = e.clientY;
        startTx = tx;
        startTy = ty;
        try {
            stage.setPointerCapture(e.pointerId);
        } catch (err) {
            // best-effort
        }
        stage.classList.add("is-grabbing");
    });
    stage.addEventListener("pointermove", function (e) {
        if (!dragging) {
            return;
        }
        const dx = e.clientX - startX;
        const dy = e.clientY - startY;
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
            moved = true;
        }
        tx = startTx + dx;
        ty = startTy + dy;
        apply();
    });
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);

    // double-click toggles a quick 2.5x at the cursor
    stage.addEventListener("dblclick", function (e) {
        e.preventDefault();
        const c = stageCenter(e);
        if (scale > 1.001) {
            reset();
        } else {
            zoomTo(2.5, c.x, c.y);
        }
    });

    // keyboard: Esc closes, +/-/0 zoom. Scoped to when open.
    document.addEventListener("keydown", function (e) {
        if (!overlay.classList.contains("is-open")) {
            return;
        }
        if (e.key === "Escape") {
            e.preventDefault();
            close();
        } else if (e.key === "+" || e.key === "=") {
            e.preventDefault();
            zoomTo(scale + STEP, 0, 0);
        } else if (e.key === "-" || e.key === "_") {
            e.preventDefault();
            zoomTo(scale - STEP, 0, 0);
        } else if (e.key === "0") {
            e.preventDefault();
            reset();
        }
    });
}

function open(src, alt) {
    if (!src) {
        return;
    }
    if (!overlay) {
        buildOverlay();
    }
    img.src = src;
    img.alt = alt || "";
    overlay.classList.add("is-open");
    overlay.setAttribute("aria-hidden", "false");
    document.body.classList.add("etp-zoom-lock");
    reset();
    const c = overlay.querySelector(".etp-zoom-close");
    if (c) {
        try {
            c.focus();
        } catch (err) {
            // focus is best-effort
        }
    }
}

function close() {
    if (!overlay) {
        return;
    }
    overlay.classList.remove("is-open");
    overlay.classList.remove("is-zoomed");
    overlay.setAttribute("aria-hidden", "true");
    document.body.classList.remove("etp-zoom-lock");
    scale = 1;
    tx = 0;
    ty = 0;
    img.style.transform = "";
    img.removeAttribute("src");
}

// Single delegated listener: any click that lands inside an .etp-image-zoomable
// node (present or future, in any backend Html field) opens the viewer with
// that node's inner <img> src. We never change the thumbnail src, never open a
// new tab, never add an anchor.
function onDocumentClick(e) {
    const target = e.target;
    if (!target || !target.closest) {
        return;
    }
    const zoomable = target.closest(".etp-image-zoomable");
    if (!zoomable) {
        return;
    }
    const thumb = zoomable.querySelector("img");
    if (!thumb) {
        return;
    }
    e.preventDefault();
    open(thumb.getAttribute("src"), thumb.getAttribute("alt"));
}

function onDocumentKeydown(e) {
    // Enter / Space on a focused thumbnail opens it (a11y parity with portal).
    if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") {
        return;
    }
    const active = document.activeElement;
    if (!active || !active.classList || !active.classList.contains("etp-image-zoomable")) {
        return;
    }
    const thumb = active.querySelector("img");
    if (!thumb) {
        return;
    }
    e.preventDefault();
    open(thumb.getAttribute("src"), thumb.getAttribute("alt"));
}

// Bind once for the lifetime of the backend session.
if (!window.__etpImageZoomReady) {
    window.__etpImageZoomReady = true;
    document.addEventListener("click", onDocumentClick);
    document.addEventListener("keydown", onDocumentKeydown);
}

// ---------------------------------------------------------------------------
// Dashboard progress bars: pin the "X / Y" pill to the end of the coloured fill.
// Odoo's progressbar renders the track and its value label as flex siblings, so
// the label floats at the track's right edge, not the fill's end. We publish
// each bar's fill ratio (from aria-valuenow/valuemax) as an --etp-fill CSS var
// on the .o_progressbar and let the stylesheet position the label at that
// offset. A MutationObserver reapplies it across widget re-renders. Piggybacks
// on this already-loaded controller so no extra asset module is needed.
// ---------------------------------------------------------------------------
function syncProgressFill() {
    const bars = document.querySelectorAll(".etp-bar-row .o_progressbar");
    for (let i = 0; i < bars.length; i++) {
        const prog = bars[i].querySelector(".o_progress");
        if (!prog) {
            continue;
        }
        const now = parseFloat(prog.getAttribute("aria-valuenow")) || 0;
        const max = parseFloat(prog.getAttribute("aria-valuemax")) || 0;
        const pct = max > 0 ? Math.min(100, Math.max(0, (now / max) * 100)) : 0;
        bars[i].style.setProperty("--etp-fill", pct + "%");
    }
}

if (!window.__etpProgressFillReady) {
    window.__etpProgressFillReady = true;
    let scheduled = false;
    const onMutate = function () {
        if (scheduled) {
            return;
        }
        scheduled = true;
        window.requestAnimationFrame(function () {
            scheduled = false;
            syncProgressFill();
        });
    };
    const startProgressFill = function () {
        if (!document.body) {
            document.addEventListener("DOMContentLoaded", startProgressFill, { once: true });
            return;
        }
        new MutationObserver(onMutate).observe(document.body, {
            childList: true,
            subtree: true,
        });
        syncProgressFill();
    };
    startProgressFill();
}
