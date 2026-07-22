/** @odoo-module */
import { registry } from "@web/core/registry";
import { FloatField, floatField } from "@web/views/fields/float/float_field";
import { useEffect, useRef } from "@odoo/owl";

/**
 * A percentage score input (0–100) that highlights itself red — and blocks the save —
 * when it is INVALID or REQUIRED-BUT-EMPTY.
 *
 * Odoo core will not flag an empty NUMBER as a missing required field (it treats 0 as
 * "filled"), and it only validates a required field when the field is editable. So we
 * enforce it here, mirroring the server rule in _check_baseline_metrics:
 *   - out of 0–100 -> invalid (always);
 *   - empty (0) once generation is Done -> invalid (that is when a score is required).
 * The check runs whenever the value / generation status / editability changes and
 * marks the field via setInvalidField, so it participates in the record's validity
 * exactly like a native required field: red box + "invalid fields" on save.
 *
 * UX: the input is right-aligned with tabular figures for readability, and its
 * content is SELECTED on focus so an existing value can be overtyped directly —
 * no need to clear the field first.
 */
export class Kensei2ScoreField extends FloatField {
    setup() {
        super.setup();
        // The numeric input carries this ref in web.FloatField's template.
        const inputRef = useRef("numpadDecimal");
        useEffect(
            (input) => {
                if (!input) {
                    return;
                }
                input.classList.add("o_pt_score_input");
                // Select everything on focus so typing replaces the current value
                // instead of appending to it — edit without clearing first.
                const onFocus = () => input.select();
                input.addEventListener("focus", onFocus);
                return () => input.removeEventListener("focus", onFocus);
            },
            () => [inputRef.el]
        );
        useEffect(
            () => {
                const rec = this.props.record;
                const value = rec.data[this.props.name] || 0;
                const genDone = rec.data.baseline_gen_status === "done";
                const invalid =
                    !this.props.readonly &&
                    (value < 0 || value > 100 || (genDone && !value));
                if (invalid) {
                    rec.setInvalidField(this.props.name);
                } else {
                    rec.resetFieldValidity(this.props.name);
                }
            },
            () => [
                this.props.record.data[this.props.name],
                this.props.record.data.baseline_gen_status,
                this.props.readonly,
            ]
        );
    }
}

export const kensei2ScoreField = {
    ...floatField,
    component: Kensei2ScoreField,
    // A percentage is entered as a plain number: don't pad it with trailing
    // zeros (so "85" stays "85" instead of becoming "85.00"); a real decimal
    // like "85.5" is still kept.
    extractProps(...args) {
        return { ...floatField.extractProps(...args), trailingZeros: false };
    },
};

registry.category("fields").add("project_tracker_score", kensei2ScoreField);
