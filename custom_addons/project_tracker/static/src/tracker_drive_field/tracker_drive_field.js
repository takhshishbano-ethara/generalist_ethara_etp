/** @odoo-module */
import { registry } from "@web/core/registry";
import { UrlField, urlField } from "@web/views/fields/url/url_field";
import { useInputField } from "@web/views/fields/input_field_hook";

// Mirrors the server-side _check_drive_links rule: a non-empty link must be a
// full http(s) URL with a real dotted host (something.tld) — "https://g" is not
// enough.
const URL_RE = /^https?:\/\/[^\s/?#]+\.[^\s/?#]{2,}([/?#]\S*)?$/i;

/**
 * A Drive Link field that flags a badly-formed URL inline.
 *
 * Same mechanism as the score field: parse() throws on an invalid value, and
 * useInputField turns that into setInvalidField — so the box goes red and the record
 * cannot be saved until it is fixed, instead of only erroring on save. Extends
 * UrlField so the read-only view is still a clickable link. setup() replaces
 * UrlField's plain input hook (which has no parse) rather than calling super.
 */
export class Kensei2DriveLinkField extends UrlField {
    setup() {
        useInputField({
            getValue: () => this.value,
            parse: (v) => {
                const val = (v || "").trim();
                if (val && !URL_RE.test(val)) {
                    throw new Error("Must be a valid http:// or https:// URL.");
                }
                return val;
            },
        });
    }
}

export const kensei2DriveLinkField = {
    ...urlField,
    component: Kensei2DriveLinkField,
};

registry.category("fields").add("project_tracker_drive_link", kensei2DriveLinkField);
