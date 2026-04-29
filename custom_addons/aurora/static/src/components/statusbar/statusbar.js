/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, useState } from "@odoo/owl";

export class AuroraStatusbar extends Component {
    static template = "aurora.AuroraStatusbar";
    static props = { ...standardFieldProps };

    get steps() {
        const fieldDef = this.props.record.fields[this.props.name];
        const allSteps = (fieldDef.selection || []).map(([value, label]) => ({
            value,
            label,
        }));
        const visible = this.visibleSteps;
        if (visible) {
            return allSteps.filter((s) => visible.has(s.value));
        }
        return allSteps.filter((s) => s.value !== "failed");
    }

    get visibleSteps() {
        const attr = this.props.record.activeFields[this.props.name]?.options?.statusbar_visible;
        if (!attr) return null;
        return new Set(attr.split(",").map((s) => s.trim()));
    }

    get currentValue() {
        return this.props.record.data[this.props.name];
    }

    get isFailed() {
        return this.currentValue === "failed";
    }

    stepState(step) {
        const steps = this.steps;
        const currentIdx = steps.findIndex((s) => s.value === this.currentValue);
        const stepIdx = steps.findIndex((s) => s.value === step.value);

        if (this.isFailed) return "failed";
        if (step.value === this.currentValue) return "current";
        if (currentIdx >= 0 && stepIdx < currentIdx) return "done";
        return "pending";
    }

    get progressPercent() {
        const steps = this.steps;
        const currentIdx = steps.findIndex((s) => s.value === this.currentValue);
        if (this.isFailed || currentIdx < 0) return 0;
        if (this.currentValue === "done") return 100;
        return Math.round(((currentIdx) / (steps.length - 1)) * 100);
    }
}

export const auroraStatusbarField = {
    component: AuroraStatusbar,
    displayName: "Aurora Statusbar",
    supportedTypes: ["selection"],
    extractProps: () => ({}),
};

registry.category("fields").add("aurora_statusbar", auroraStatusbarField);
