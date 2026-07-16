/** @odoo-module */
import { registry } from "@web/core/registry";
import { FloatField, floatField } from "@web/views/fields/float/float_field";

/**
 * A percentage score input that rejects out-of-range values inline.
 *
 * The evaluation scores must be 0–100. The base FloatField only rejects non-numbers;
 * this adds the range. By THROWING from parse() we reuse core's own invalid-input
 * path (useInputField.onChange catches it and calls setInvalidField), so the box
 * turns red and the record can't be saved until it is fixed — the same treatment a
 * required-but-empty field gets, but for a wrong value. Server-side
 * _check_baseline_metrics still enforces the same rule, so this is UX, not the
 * source of truth.
 */
export class Kensei2ScoreField extends FloatField {
    parse(value) {
        const parsed = super.parse(value);
        if (parsed < 0 || parsed > 100) {
            throw new Error("Score must be between 0 and 100.");
        }
        return parsed;
    }
}

export const kensei2ScoreField = {
    ...floatField,
    component: Kensei2ScoreField,
};

registry.category("fields").add("kensei2_score", kensei2ScoreField);
