/** @odoo-module **/
import { Component } from "@odoo/owl";

export class StepModelSelection extends Component {
    static template = "rl_gym_dashboard.TrainingWizard.StepModelSelection";
    static props = {
        models: { type: Array },
        selectedModelId: { type: [Number, { value: null }], optional: true },
        customName: { type: String },
        loading: { type: Boolean },
        onSelectModel: { type: Function },
        onNameChange: { type: Function },
    };

    selectModel(id) {
        this.props.onSelectModel(id);
    }

    onNameInput(ev) {
        this.props.onNameChange(ev.target.value);
    }

    formatParams(count) {
        if (!count) return "";
        if (count >= 1e9) return (count / 1e9).toFixed(1) + "B";
        if (count >= 1e6) return (count / 1e6).toFixed(0) + "M";
        if (count >= 1e3) return (count / 1e3).toFixed(0) + "K";
        return String(count);
    }
}
