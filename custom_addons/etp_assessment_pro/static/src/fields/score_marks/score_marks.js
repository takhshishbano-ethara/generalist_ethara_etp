import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";

/**
 * Read-only cell that DISPLAYS the whole-marks score ("4 / 10") while being
 * BOUND to the numeric score_percent, so a list column sorts by ratio, not by
 * the lexical "X / N" text. A Char sort ranks "2 / 2" (100%) below "4 / 10"
 * (40%) because "2" < "4"; binding the column to score_percent orders correctly
 * across assessments with different question counts, and score_marks_display
 * (a fieldDependency) supplies the human-readable marks. Falls back to the raw
 * percent only if the marks string is somehow absent.
 */
export class EtpScoreMarks extends Component {
    static template = "etp_assessment_pro.EtpScoreMarks";
    static props = { ...standardFieldProps };

    get marks() {
        const rec = this.props.record.data;
        if (rec.score_marks_display) {
            return rec.score_marks_display;
        }
        const pct = rec[this.props.name];
        return pct || pct === 0 ? `${Math.round(pct)}%` : "";
    }
}

export const etpScoreMarks = {
    component: EtpScoreMarks,
    displayName: _t("Score marks"),
    supportedTypes: ["float", "integer"],
    fieldDependencies: [{ name: "score_marks_display", type: "char" }],
};

registry.category("fields").add("etp_score_marks", etpScoreMarks);
