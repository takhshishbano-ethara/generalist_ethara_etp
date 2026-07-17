import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";

/**
 * Renders the resource rows of one category as inline chips: filename, a small
 * status circle, and a remove button.
 *
 * Bound to resource_ids (the real One2many) and filtered client-side by category,
 * NOT to a domain One2many: _add_resource() appends rows that a sibling domain
 * One2many cannot see until the record is saved. Reading the real list is what
 * makes a chip appear the moment a file is picked. (The same constraint is why
 * sop_resource_ids on the model has to be computed.)
 *
 * The same field may back several of these nodes (SOP + reference) plus the
 * Resources list: Odoo keys field nodes name_0/name_1/... so each renders with its
 * own options, and unions their sub-field specs via patchActiveFields.
 *
 * options:
 *   category - resource category to show ("sop", "reference", ...)
 */
export class EtpFileChips extends Component {
    static template = "etp_assessment_pro.EtpFileChips";
    static props = {
        ...standardFieldProps,
        category: { type: String },
    };

    get chips() {
        const list = this.props.record.data[this.props.name];
        if (!list || !list.records) {
            return [];
        }
        return list.records
            .filter((rec) => rec.data.category === this.props.category)
            .map((rec) => ({
                rec,
                name: rec.data.name || "",
                // status is computed and non-stored; fall back rather than render a bare dot
                status: rec.data.status || "pending",
                title: this.statusTitle(rec),
            }));
    }

    /** The dot's colour is a secondary cue - this text is the accessible signal. */
    statusTitle(rec) {
        if (rec.data.status === "failed") {
            return _t("Failed - %s", rec.data.extraction_error || _t("extraction error"));
        }
        if (rec.data.status === "ready") {
            return _t("Ready - %s characters extracted",
                (rec.data.char_count || 0).toLocaleString());
        }
        if (rec.data.status === "native") {
            return _t("Ready - read directly by the model, no text extraction needed");
        }
        return _t("Uploaded - text not extracted yet");
    }

    onRemove(chip) {
        this.props.record.data[this.props.name].delete(chip.rec);
    }
}

export const etpFileChips = {
    component: EtpFileChips,
    displayName: _t("File chips"),
    supportedTypes: ["one2many"],
    supportedOptions: [
        { label: _t("Resource category"), name: "category", type: "string" },
    ],
    extractProps: ({ options }) => ({ category: options.category }),
    relatedFields: () => [
        { name: "name", type: "char" },
        { name: "category", type: "selection" },
        { name: "status", type: "selection" },
        { name: "char_count", type: "integer" },
        { name: "extraction_error", type: "char" },
    ],
};

registry.category("fields").add("etp_file_chips", etpFileChips);
