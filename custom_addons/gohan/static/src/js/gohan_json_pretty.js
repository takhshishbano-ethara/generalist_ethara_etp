/** @odoo-module **/

// Pretty-printed JSON field widget. Stock `widget="json"` calls
// JSON.stringify(value) with no indent and renders the result inside a
// <span>, which collapses everything to one unreadable line; this widget
// renders 2-space-indented JSON inside a scrolling <pre><code> block.

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class GohanJsonPrettyField extends Component {
    static template = "gohan.JsonPrettyField";
    static props = { ...standardFieldProps };

    get formatted() {
        const value = this.props.record.data[this.props.name];

        if (value === null || value === undefined || value === false) {
            return "";
        }
        if (typeof value === "string") {
            const trimmed = value.trim();
            if (!trimmed) {
                return "";
            }
            // String fields may carry either serialised JSON (webhook
            // payloads) or free-form bullet text (extraction_warnings).
            // Try JSON first; fall back to raw text so newlines are kept.
            try {
                return JSON.stringify(JSON.parse(trimmed), null, 2);
            } catch {
                return value;
            }
        }
        try {
            return JSON.stringify(value, null, 2);
        } catch {
            return String(value);
        }
    }

    /**
     * True when the formatted value contains nothing renderable, so the
     * template can emit a faint placeholder instead of an empty <pre>.
     */
    get isEmpty() {
        const f = this.formatted;
        return !f || !f.toString().trim();
    }
}

export const gohanJsonPrettyField = {
    component: GohanJsonPrettyField,
    displayName: "Pretty JSON",
    // Accept the three field types we actually bind in gohan views.
    supportedTypes: ["json", "text", "char"],
};

registry.category("fields").add("json_pretty", gohanJsonPrettyField);
