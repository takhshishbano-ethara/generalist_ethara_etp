/** @odoo-module **/

(function () {
    "use strict";

    let lightbox = null;
    let flipState = null;

    function closeLightbox() {
        if (!lightbox) return;
        const lb = lightbox;
        lightbox = null;
        flipState = null;
        lb.setAttribute("data-open", "false");
        setTimeout(() => {
            if (lb.parentNode) lb.parentNode.removeChild(lb);
        }, 250);
    }

    function buildBaseLightbox(ariaLabel) {
        const lb = document.createElement("div");
        lb.className = "i2i-lightbox";
        lb.setAttribute("role", "dialog");
        lb.setAttribute("aria-modal", "true");
        lb.setAttribute("aria-label", ariaLabel);

        const closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.className = "i2i-lightbox-close";
        closeBtn.setAttribute("aria-label", "Close");
        closeBtn.textContent = "\u00d7";
        closeBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            closeLightbox();
        });

        lb.appendChild(closeBtn);
        lb.addEventListener("click", (e) => {
            if (e.target === lb) closeLightbox();
        });

        return lb;
    }

    function openLightbox(imgSrc, altText) {
        if (lightbox) closeLightbox();

        const lb = buildBaseLightbox("Image preview");

        const img = document.createElement("img");
        img.src = imgSrc;
        img.alt = altText || "";

        lb.appendChild(img);
        document.body.appendChild(lb);
        lightbox = lb;

        requestAnimationFrame(() => {
            if (lightbox === lb) lb.setAttribute("data-open", "true");
        });
    }

    function applyFlipState() {
        if (!flipState) return;
        flipState.imgEl.src = flipState.isOriginal
            ? flipState.origSrc
            : flipState.editedSrc;
        flipState.imgEl.alt = flipState.isOriginal ? "Original" : "Edited";
        flipState.labelEl.textContent = flipState.isOriginal
            ? "ORIGINAL"
            : "EDITED";
    }

    function toggleFlip() {
        if (!flipState) return;
        flipState.isOriginal = !flipState.isOriginal;
        applyFlipState();
    }

    function openFlipLightbox(origSrc, editedSrc) {
        if (lightbox) closeLightbox();

        const lb = buildBaseLightbox("Flip compare preview");
        lb.classList.add("i2i-flip-lightbox");

        const label = document.createElement("div");
        label.className = "i2i-flip-lightbox-label";
        label.textContent = "ORIGINAL";

        const hint = document.createElement("div");
        hint.className = "i2i-flip-lightbox-hint";
        hint.textContent = "Click image or press F to flip";

        const img = document.createElement("img");
        img.src = origSrc;
        img.alt = "Original";
        img.addEventListener("click", (e) => {
            e.stopPropagation();
            toggleFlip();
        });

        lb.appendChild(label);
        lb.appendChild(img);
        lb.appendChild(hint);
        document.body.appendChild(lb);
        lightbox = lb;

        flipState = {
            origSrc: origSrc,
            editedSrc: editedSrc,
            isOriginal: true,
            imgEl: img,
            labelEl: label,
        };

        requestAnimationFrame(() => {
            if (lightbox === lb) lb.setAttribute("data-open", "true");
        });
    }

    document.addEventListener(
        "click",
        (e) => {
            const flipBtn = e.target.closest && e.target.closest(".i2i-flip-compare-btn");
            if (flipBtn) {
                const container = flipBtn.closest(".i2i-image-compare-section");
                if (!container) return;
                const imgs = container.querySelectorAll(".i2i-image-zoom img");
                if (imgs.length < 2) return;
                const origSrc = imgs[0].currentSrc || imgs[0].src;
                const editedSrc = imgs[1].currentSrc || imgs[1].src;
                if (!origSrc || !editedSrc) return;
                e.preventDefault();
                e.stopPropagation();
                openFlipLightbox(origSrc, editedSrc);
                return;
            }

            const zoom = e.target.closest && e.target.closest(".i2i-image-zoom");
            if (!zoom) return;
            const img = zoom.querySelector("img");
            if (!img) return;
            const src = img.currentSrc || img.src;
            if (!src) return;
            e.preventDefault();
            e.stopPropagation();
            openLightbox(src, img.alt);
        },
        true
    );

    document.addEventListener("keydown", (e) => {
        if (!lightbox) return;
        if (e.key === "Escape") {
            closeLightbox();
            return;
        }
        if (flipState && (e.key === "f" || e.key === "F")) {
            e.preventDefault();
            toggleFlip();
        }
    });
})();
