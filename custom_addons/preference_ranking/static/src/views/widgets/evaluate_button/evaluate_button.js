/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { Component, useState, useRef } from "@odoo/owl";

export class EvaluateButton extends Component {
    static template = "preference_ranking.EvaluateButton";
    static props = { ...standardWidgetProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({ evaluating: false });
        this.rootRef = useRef("root");
    }

    get record() {
        return this.props.record;
    }

    async onClickEvaluate() {
        if (this.state.evaluating) return;

        const record = this.record;
        const resId = record.resId;

        if (!resId) {
            this.notification.add(_t("Please save the record first."), { type: "warning" });
            return;
        }

        // Save the record first so eval_task reads current field values
        try {
            await record.save();
        } catch (e) {
            this.notification.add(_t("Failed to save. Please fix errors first."), { type: "danger" });
            return;
        }

        // Lock the form fields
        this.state.evaluating = true;
        this._setFormFieldsLocked(true);

        try {
            // Call eval_task via RPC (no page reload)
            await this.orm.call(
                "preference.ranking",
                "evaluate_task",
                [[resId]]
            );

            // Reload the record to pick up all changes from eval_task
            await record.load();
        } catch (error) {
            console.error("Evaluation failed:", error);
            this.notification.add(
                _t("Evaluation failed: ") + (error.message || error.data?.message || "Unknown error"),
                { type: "danger", sticky: true }
            );
        } finally {
            // Unlock the form fields
            this.state.evaluating = false;
            this._setFormFieldsLocked(false);
        }
    }

    _setFormFieldsLocked(locked) {
        const el = this.rootRef.el;
        const formEl = el?.closest(".o_form_view") || document.querySelector(".o_form_view");
        if (!formEl) return;

        if (locked) {
            formEl.classList.add("o_evaluating_locked");
        } else {
            formEl.classList.remove("o_evaluating_locked");
        }
    }
}

registry.category("view_widgets").add("evaluate_button", {
    component: EvaluateButton,
});
