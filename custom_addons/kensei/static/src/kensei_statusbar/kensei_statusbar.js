/** @odoo-module */
import { registry } from "@web/core/registry";
import { useEffect } from "@odoo/owl";
import { StatusBarField, statusBarField } from "@web/views/fields/statusbar/statusbar_field";

/**
 * Statusbar variant that flags the stages BEFORE the current one as "done".
 *
 * It reuses the core statusbar template unchanged and, after each render, adds
 * an ``o_kensei_stage_done`` class to the buttons whose ``data-value`` comes
 * before the selected value in the field's selection order. Because it keys off
 * the selection order + each button's own data-value — not the DOM order of the
 * arrow buttons — the "completed = green" styling is robust to Odoo's (reversed)
 * statusbar DOM layout, unlike a `~`/`:has` sibling-order CSS rule.
 *
 * Scoped to `widget="kensei_statusbar"`; every other statusbar is untouched.
 */
export class KenseiStatusBarField extends StatusBarField {
    setup() {
        super.setup();
        useEffect(() => this._markDone());
    }

    _markDone() {
        const root = this.rootRef && this.rootRef.el;
        if (!root) {
            return;
        }
        const items = this.getAllItems();
        const selected = items.findIndex((i) => i.isSelected);
        const done = new Set(
            selected > 0 ? items.slice(0, selected).map((i) => String(i.value)) : []
        );
        for (const btn of root.querySelectorAll(".o_arrow_button")) {
            btn.classList.toggle("o_kensei_stage_done", done.has(btn.dataset.value));
        }
    }
}

export const kenseiStatusBarField = {
    ...statusBarField,
    component: KenseiStatusBarField,
    // Odoo names the wrapper class after the *widget*, so without this the field
    // misses core's `.o_field_statusbar` stylesheet — including the `row-reverse`
    // that un-reverses getSortedItems()'s deliberately reversed DOM.
    additionalClasses: ["o_field_statusbar"],
};

registry.category("fields").add("kensei_statusbar", kenseiStatusBarField);
