/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { Component, useState, useRef, onWillUnmount } from "@odoo/owl";

const LOCK_CSS_CLASS = "valor-action-running";
const SAFETY_TIMEOUT_MS = 10 * 60 * 1000;

let _lockOwner = null;
let _safetyTimer = null;

function _forceUnlock() {
    const formEl = document.querySelector(`.o_form_view.${LOCK_CSS_CLASS}`);
    if (formEl) {
        formEl.classList.remove(LOCK_CSS_CLASS);
    }
    _lockOwner = null;
    if (_safetyTimer) {
        clearTimeout(_safetyTimer);
        _safetyTimer = null;
    }
}

const BUTTON_CONFIGS = {
    evaluate_button1: { label: "Evaluation", loadingLabel: "Evaluating..." },
    evaluate_button2: { label: "Evaluation", loadingLabel: "Evaluating..." },
    evaluate_button3: { label: "Evaluation", loadingLabel: "Evaluating..." },
    evaluate_button4: { label: "Evaluation", loadingLabel: "Evaluating..." },
    evaluate_button5: { label: "Evaluation", loadingLabel: "Evaluating..." },
    evaluate_button6: { label: "Evaluation", loadingLabel: "Evaluating..." },
    evaluate_button7: { label: "Evaluation", loadingLabel: "Evaluating..." },
    action_turn2: { label: "Turn 2", loadingLabel: "Loading Turn 2..." },
    action_turn3: { label: "Turn 3", loadingLabel: "Loading Turn 3..." },
    action_turn4: { label: "Turn 4", loadingLabel: "Loading Turn 4..." },
    action_turn5: { label: "Turn 5", loadingLabel: "Loading Turn 5..." },
    action_turn6: { label: "Turn 6", loadingLabel: "Loading Turn 6..." },
    action_turn7: { label: "Turn 7", loadingLabel: "Loading Turn 7..." },
    action_submit_prompt1: { label: "Submit Prompt", loadingLabel: "Generating responses..." },
    action_submit_prompt2: { label: "Submit Prompt", loadingLabel: "Generating responses..." },
    action_submit_prompt3: { label: "Submit Prompt", loadingLabel: "Generating responses..." },
    action_submit_prompt4: { label: "Submit Prompt", loadingLabel: "Generating responses..." },
    action_submit_prompt5: { label: "Submit Prompt", loadingLabel: "Generating responses..." },
    action_submit_prompt6: { label: "Submit Prompt", loadingLabel: "Generating responses..." },
    action_submit_prompt7: { label: "Submit Prompt", loadingLabel: "Generating responses..." },
    submit_task: { label: "Submit", loadingLabel: "Submitting..." },
};

function makeValorButton(methodName) {
    const { label, loadingLabel } = BUTTON_CONFIGS[methodName];

    class ValorActionButton extends Component {
        static template = "valor.ActionButton";
        static props = { ...standardWidgetProps };

        setup() {
            this.orm = useService("orm");
            this.notification = useService("notification");
            this.state = useState({ loading: false });
            this.rootRef = useRef("root");
            this._instanceId = `${methodName}_${Math.random().toString(36).slice(2)}`;

            onWillUnmount(() => {
                if (_lockOwner === this._instanceId) {
                    _forceUnlock();
                }
            });
        }

        get record() {
            return this.props.record;
        }

        get buttonLabel() {
            return label;
        }

        get loadingLabel() {
            return loadingLabel;
        }

        async onClick() {
            if (this.state.loading) return;

            const record = this.record;
            const resId = record.resId;

            if (!resId) {
                this.notification.add(_t("Please save the record first."), { type: "warning" });
                return;
            }

            try {
                await record.save();
            } catch (e) {
                this.notification.add(_t("Failed to save. Please fix errors first."), { type: "danger" });
                return;
            }

            this.state.loading = true;
            this._lockForm();

            try {
                await this.orm.call("valor", methodName, [[resId]]);
                await record.load();
            } catch (error) {
                this.notification.add(
                    _t("Action failed: ") + (error.message || error.data?.message || _t("Unknown error")),
                    { type: "danger", sticky: true }
                );
            } finally {
                this.state.loading = false;
                this._unlockForm();
            }
        }

        _lockForm() {
            const el = this.rootRef.el;
            const formEl = el?.closest(".o_form_view") || document.querySelector(".o_form_view");
            if (!formEl) return;

            _lockOwner = this._instanceId;
            formEl.classList.add(LOCK_CSS_CLASS);

            if (_safetyTimer) clearTimeout(_safetyTimer);
            _safetyTimer = setTimeout(() => _forceUnlock(), SAFETY_TIMEOUT_MS);
        }

        _unlockForm() {
            if (_lockOwner !== this._instanceId) return;
            _forceUnlock();
        }
    }

    return ValorActionButton;
}

for (const methodName of Object.keys(BUTTON_CONFIGS)) {
    registry.category("view_widgets").add(methodName, {
        component: makeValorButton(methodName),
    });
}
