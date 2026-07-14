/** @odoo-module */
import { registry } from "@web/core/registry";
import { useEffect } from "@odoo/owl";
import { StatusBarField, statusBarField } from "@web/views/fields/statusbar/statusbar_field";

/**
 * Statusbar variant that flags the stages BEFORE the current one as "done", and
 * gives every stage button a tooltip carrying its full name.
 *
 * It reuses the core statusbar template unchanged and, after each render, adds
 * an ``o_kensei2_stage_done`` class to the buttons whose ``data-value`` comes
 * before the selected value in the field's selection order. Because it keys off
 * the selection order + each button's own data-value — not the DOM order of the
 * arrow buttons — the "completed = green" styling is robust to Odoo's (reversed)
 * statusbar DOM layout, unlike a `~`/`:has` sibling-order CSS rule.
 *
 * Scoped to `widget="kensei2_statusbar"`; every other statusbar is untouched.
 */
export class Kensei2StatusBarField extends StatusBarField {
    setup() {
        super.setup();
        useEffect(() => this._decorateButtons());
    }

    _decorateButtons() {
        const root = this.rootRef && this.rootRef.el;
        if (!root) {
            return;
        }
        const items = this.getAllItems();
        const selected = items.findIndex((i) => i.isSelected);
        const done = new Set(
            selected > 0 ? items.slice(0, selected).map((i) => String(i.value)) : []
        );
        const labels = new Map(items.map((i) => [String(i.value), i.label]));

        for (const btn of root.querySelectorAll(".o_arrow_button")) {
            const value = btn.dataset.value;
            btn.classList.toggle("o_kensei2_stage_done", done.has(value));
            // Core's statusbar template sets no title. The stage names are kept
            // short so they fit on one row (see STATUS_SELECTION), but on a narrow
            // window the CSS ellipsizes them — so carry the full name in a tooltip
            // rather than leaving a clipped label with no way to read it.
            const label = labels.get(value);
            if (label) {
                btn.setAttribute("title", label);
            }
        }
    }
}

export const kensei2StatusBarField = {
    ...statusBarField,
    component: Kensei2StatusBarField,
    // Odoo names the wrapper class after the *widget*, so without this the field
    // misses core's `.o_field_statusbar` stylesheet — including the `row-reverse`
    // that un-reverses getSortedItems()'s deliberately reversed DOM.
    additionalClasses: ["o_field_statusbar"],
};

registry.category("fields").add("kensei2_statusbar", kensei2StatusBarField);
