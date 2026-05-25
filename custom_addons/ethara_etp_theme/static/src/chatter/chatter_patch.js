/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { Chatter } from "@mail/chatter/web_portal/chatter";
import { FormRenderer } from "@web/views/form/form_renderer";
import { onMounted, onWillUnmount } from "@odoo/owl";

const STORAGE_KEY = "ethara_chatter_width";
const MIN_WIDTH = 320;
const MAX_RATIO = 0.6;

patch(FormRenderer.prototype, {
    mailLayout(hasAttachmentContainer) {
        const layout = super.mailLayout(hasAttachmentContainer);
        if (document.documentElement.dataset.etharaChatter !== "bottom") {
            return layout;
        }
        if (layout === "SIDE_CHATTER") {
            return "BOTTOM_CHATTER";
        }
        if (layout === "EXTERNAL_COMBO_XXL") {
            return "EXTERNAL_COMBO";
        }
        return layout;
    },
});

patch(Chatter.prototype, {
    setup() {
        super.setup();
        this._etharaResize = {
            dragging: false,
            startX: 0,
            startWidth: 0,
            onPointerMove: null,
            onPointerUp: null,
        };
        onMounted(() => this._etharaRestoreWidth());
        onWillUnmount(() => this._etharaTeardown());
    },

    _etharaRestoreWidth() {
        if (!this.props.isChatterAside) {
            return;
        }
        const saved = parseInt(window.localStorage.getItem(STORAGE_KEY), 10);
        if (saved && !Number.isNaN(saved)) {
            this._etharaApplyWidth(saved);
        }
    },

    _etharaTeardown() {
        const r = this._etharaResize;
        if (r.onPointerMove) {
            window.removeEventListener("pointermove", r.onPointerMove);
        }
        if (r.onPointerUp) {
            window.removeEventListener("pointerup", r.onPointerUp);
        }
        document.documentElement.classList.remove("o_ethara_chatter_resizing");
    },

    _etharaClamp(px) {
        const max = Math.floor(window.innerWidth * MAX_RATIO);
        return Math.max(MIN_WIDTH, Math.min(px, max));
    },

    _etharaApplyWidth(px) {
        const clamped = this._etharaClamp(px);
        document.documentElement.style.setProperty(
            "--ethara-chatter-width",
            `${clamped}px`,
        );
        return clamped;
    },

    onEtharaResizeStart(ev) {
        if (!this.props.isChatterAside || ev.button !== 0) {
            return;
        }
        const wrapper = this.rootRef.el?.closest(".o-mail-Form-chatter");
        if (!wrapper) {
            return;
        }
        ev.preventDefault();
        const r = this._etharaResize;
        r.dragging = true;
        r.startX = ev.clientX;
        r.startWidth = wrapper.getBoundingClientRect().width;
        document.documentElement.classList.add("o_ethara_chatter_resizing");

        r.onPointerMove = (mEv) => {
            if (!r.dragging) return;
            const delta = r.startX - mEv.clientX;
            this._etharaApplyWidth(r.startWidth + delta);
        };
        r.onPointerUp = () => {
            if (!r.dragging) return;
            r.dragging = false;
            document.documentElement.classList.remove("o_ethara_chatter_resizing");
            const current = parseInt(
                getComputedStyle(document.documentElement)
                    .getPropertyValue("--ethara-chatter-width"),
                10,
            );
            if (current && !Number.isNaN(current)) {
                window.localStorage.setItem(STORAGE_KEY, String(current));
            }
            window.removeEventListener("pointermove", r.onPointerMove);
            window.removeEventListener("pointerup", r.onPointerUp);
            r.onPointerMove = null;
            r.onPointerUp = null;
        };
        window.addEventListener("pointermove", r.onPointerMove);
        window.addEventListener("pointerup", r.onPointerUp);
    },

    onEtharaResizeDoubleClick() {
        if (!this.props.isChatterAside) {
            return;
        }
        window.localStorage.removeItem(STORAGE_KEY);
        document.documentElement.style.removeProperty("--ethara-chatter-width");
    },
});
