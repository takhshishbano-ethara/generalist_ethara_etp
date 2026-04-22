/** @odoo-module */

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

const STEPS = [
    {
        key: "qc",
        label: "QC Check",
        field: "qc_status",
        iconMap: {
            pending: "fa-circle-o",
            passed: "fa-check-circle",
            failed: "fa-times-circle",
        },
        comingSoon: false,
    },
    {
        key: "claude",
        label: "Claude 4.7",
        field: "claude_session_status",
        iconMap: {
            not_started: "fa-microchip",
            in_progress: "fa-microchip",
            completed: "fa-check-circle",
        },
        comingSoon: false,
    },
    {
        key: "glm",
        label: "GLM 5",
        field: "glm_session_status",
        iconMap: {
            not_started: "fa-microchip",
            in_progress: "fa-microchip",
            completed: "fa-check-circle",
        },
        comingSoon: false,
    },
    {
        key: "oneP",
        label: "1P",
        field: "oneP_session_status",
        iconMap: {
            not_started: "fa-microchip",
            in_progress: "fa-microchip",
            completed: "fa-check-circle",
        },
        comingSoon: false,
    },
];

export class TaskProgressWidget extends Component {
    static template = "talos.TaskProgressWidget";
    static props = { ...standardWidgetProps };

    get steps() {
        const data = this.props.record.data;
        return STEPS.map((step) => {
            const rawValue = data[step.field];
            const status = rawValue || (step.key === "qc" ? "pending" : "not_started");

            let state;
            if (step.comingSoon) {
                state = "coming_soon";
            } else if (status === "passed" || status === "completed") {
                state = "done";
            } else if (status === "failed") {
                state = "failed";
            } else if (status === "in_progress") {
                state = "active";
            } else {
                state = "idle";
            }

            const icon = step.comingSoon
                ? "fa-microchip"
                : step.iconMap[status] || "fa-circle-o";

            return {
                key: step.key,
                label: step.label,
                icon,
                state,
                comingSoon: step.comingSoon,
            };
        });
    }

    get connectors() {
        const s = this.steps;
        const result = [];
        for (let i = 0; i < s.length - 1; i++) {
            const from = s[i];
            const to = s[i + 1];
            const filled = from.state === "done" && (to.state === "done" || to.state === "active");
            result.push({ key: `${from.key}-${to.key}`, filled });
        }
        return result;
    }

    stepClass(step) {
        return `o_talos_progress_step o_talos_progress_step_${step.state}`;
    }

    connectorClass(conn) {
        return `o_talos_progress_connector ${conn.filled ? "o_talos_progress_connector_filled" : ""}`;
    }
}

export const taskProgressWidgetDef = { component: TaskProgressWidget };
registry.category("view_widgets").add("talos_task_progress", taskProgressWidgetDef);
