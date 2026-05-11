/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component } from "@odoo/owl";

const PHASES = [
    { key: "build", label: "Build", icon: "fa-cube" },
    { key: "run", label: "Run", icon: "fa-rocket" },
];

export class Commit0Stepper extends Component {
    static template = "kaiju_commit0.Commit0Stepper";
    static props = { ...standardFieldProps };

    get phases() {
        const buildStatus = this.props.record.data.build_status;
        const runCount = this.props.record.data.run_count || 0;
        const hasCompletedRun = runCount > 0;

        return PHASES.map((phase, idx) => {
            let state = "pending";

            if (phase.key === "build") {
                if (buildStatus === "done") state = "done";
                else if (buildStatus === "running") state = "running";
                else if (buildStatus === "failed") state = "failed";
            } else if (phase.key === "run") {
                if (hasCompletedRun) state = "done";
                else if (buildStatus === "done") state = "pending";
            }

            return {
                ...phase,
                state,
                active: phase.key === "build" ? buildStatus !== "done" : buildStatus === "done",
                isLast: idx === PHASES.length - 1,
            };
        });
    }
}

export const commit0StepperField = {
    component: Commit0Stepper,
    displayName: "Phase Stepper",
    supportedTypes: ["selection"],
    extractProps: () => ({}),
};

registry.category("fields").add("commit0_stepper", commit0StepperField);
