/**
 * Drag-to-resize for the ETP form chatter (Generators + Assessments).
 *
 * The chatter is pinned to the right of the form by backend.scss with a narrow
 * default width driven by the --etp-chatter-width CSS variable. This controller
 * lets the user drag the chatter's LEFT edge to make it wider/narrower and
 * remembers the chosen width across reloads (localStorage).
 *
 * Why a global delegated controller (not an OWL patch): the chatter is rendered
 * and frequently re-rendered by OWL, so any element we inject gets wiped. A
 * MutationObserver re-injects the drag handle whenever the chatter reappears,
 * and a single document-level pointer listener (event delegation) drives the
 * drag no matter how many times the chatter is re-created. Nothing here renders
 * until an ETP form with a chatter is on screen.
 *
 * Scope: only .o_etp_backend.o_form_view forms (this module's Generators +
 * Assessments), matching the SCSS selectors exactly, so no other app is touched.
 */

const LS_KEY = "etp_chatter_width_v2";
const MIN_W = 240;
// Match the chatter/renderer under BOTH Odoo layouts: .flex-column (narrow
// screens) and .flex-nowrap (wide monitors, at/above Odoo's XXL breakpoint).
// The old selectors only matched .flex-column, so on wide screens the handle
// never got injected and the width override never applied.
const CHATTER_SEL =
    ".o_etp_backend.o_form_view .o_form_renderer > .o-mail-Form-chatter";
const RENDERER_SEL =
    ".o_etp_backend.o_form_view .o_form_renderer";

let dragging = null; // { rightEdge } captured at pointerdown

function maxW() {
    // never let the chatter eat the whole form
    return Math.round(window.innerWidth * 0.9);
}

function clampWidth(px) {
    return Math.max(MIN_W, Math.min(maxW(), Math.round(px)));
}

function savedWidth() {
    try {
        const v = parseInt(window.localStorage.getItem(LS_KEY), 10);
        return Number.isFinite(v) ? v : null;
    } catch (err) {
        return null;
    }
}

function persistWidth(px) {
    try {
        window.localStorage.setItem(LS_KEY, String(px));
    } catch (err) {
        // localStorage may be unavailable (private mode) - resize still works
        // for the session, it just will not be remembered.
    }
}

// Push the width onto every matching renderer as an inline CSS variable; the
// SCSS rule reads var(--etp-chatter-width) on the chatter child.
function applyWidth(px) {
    const renderers = document.querySelectorAll(RENDERER_SEL);
    for (const r of renderers) {
        r.style.setProperty("--etp-chatter-width", px + "px");
    }
}

function hasDirectResizer(chatter) {
    for (const c of chatter.children) {
        if (c.classList && c.classList.contains("etp-chatter-resizer")) {
            return true;
        }
    }
    return false;
}

// Ensure every on-screen ETP chatter has a left-edge drag handle, and (re)apply
// the saved width. Cheap + idempotent so it is safe to call on every mutation.
function ensureHandles() {
    const chatters = document.querySelectorAll(CHATTER_SEL);
    for (const chatter of chatters) {
        if (!hasDirectResizer(chatter)) {
            const handle = document.createElement("div");
            handle.className = "etp-chatter-resizer";
            handle.setAttribute("title", "Drag to resize the chatter");
            handle.setAttribute("role", "separator");
            handle.setAttribute("aria-orientation", "vertical");
            // always-visible two-sided-arrow grip so the affordance is obvious
            const grip = document.createElement("span");
            grip.className = "etp-chatter-grip";
            grip.setAttribute("aria-hidden", "true");
            // FontAwesome arrows-h (<->); the glyph renders even if FA is late.
            grip.innerHTML = '<i class="fa fa-arrows-h"></i>';
            handle.appendChild(grip);
            chatter.prepend(handle);
        }
    }
    // Do not fight an in-flight drag; only re-assert the stored width at rest.
    if (!dragging) {
        const w = savedWidth();
        if (w !== null) {
            applyWidth(clampWidth(w));
        }
    }
}

function onPointerDown(e) {
    const handle =
        e.target && e.target.closest ? e.target.closest(".etp-chatter-resizer") : null;
    if (!handle) {
        return;
    }
    const chatter = handle.parentElement;
    if (!chatter) {
        return;
    }
    e.preventDefault();
    // The chatter's right edge is pinned to the form's right edge and stays put
    // as the width changes, so capture it once: width = rightEdge - pointerX.
    const rect = chatter.getBoundingClientRect();
    dragging = { rightEdge: rect.right };
    handle.classList.add("is-dragging");
    document.body.classList.add("etp-chatter-resizing");
    try {
        handle.setPointerCapture(e.pointerId);
    } catch (err) {
        // pointer capture is best-effort
    }
}

function onPointerMove(e) {
    if (!dragging) {
        return;
    }
    applyWidth(clampWidth(dragging.rightEdge - e.clientX));
}

function endDrag() {
    if (!dragging) {
        return;
    }
    dragging = null;
    // Read the width that actually landed and persist it.
    const chatter = document.querySelector(CHATTER_SEL);
    if (chatter) {
        const px = parseInt(window.getComputedStyle(chatter).width, 10);
        if (Number.isFinite(px)) {
            persistWidth(px);
        }
    }
    for (const h of document.querySelectorAll(".etp-chatter-resizer.is-dragging")) {
        h.classList.remove("is-dragging");
    }
    document.body.classList.remove("etp-chatter-resizing");
}

function onWindowResize() {
    // keep a previously-saved width within the new viewport bounds
    const w = savedWidth();
    if (w !== null) {
        applyWidth(clampWidth(w));
    }
}

if (!window.__etpChatterResizeReady) {
    window.__etpChatterResizeReady = true;

    const init = () => {
        // document.body must exist before we observe it; the backend bundle can
        // evaluate while the <body> is still being parsed, so defer if needed.
        if (!document.body) {
            document.addEventListener("DOMContentLoaded", init, { once: true });
            return;
        }

        document.addEventListener("pointerdown", onPointerDown);
        document.addEventListener("pointermove", onPointerMove);
        document.addEventListener("pointerup", endDrag);
        document.addEventListener("pointercancel", endDrag);
        window.addEventListener("resize", onWindowResize);

        // Re-inject the handle + re-apply width whenever OWL re-renders the
        // chatter. Debounced to one pass per animation frame so a burst of
        // mutations is cheap.
        let scheduled = false;
        const observer = new MutationObserver(() => {
            if (scheduled) {
                return;
            }
            scheduled = true;
            window.requestAnimationFrame(() => {
                scheduled = false;
                ensureHandles();
            });
        });
        observer.observe(document.body, { childList: true, subtree: true });

        // first pass in case a form is already open
        ensureHandles();
    };

    init();
}
