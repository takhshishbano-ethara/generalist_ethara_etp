/** @odoo-module */

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

const STAGES = [
    { key: "stage1", label: "Validation", field: "crawl_status" },
    { key: "stage2", label: "PR Collection", field: "pr_collection_status" },
    { key: "stage3", label: "Docker Build", field: "docker_build_status" },
    { key: "stage4", label: "Test Execution", field: "test_execution_status" },
    { key: "stage5", label: "Dataset", field: "dataset_status" },
];

const STAGE_INDEX = {
    stage1: 0,
    stage2: 1,
    stage3: 2,
    stage4: 3,
    stage5: 4,
    stage6: 5,
    stage7: 6,
    done: 7,
    failed: -1,
};

const PROGRESS_FIELDS = {
    stage2: "pr_collection_progress",
    stage3: "docker_build_progress",
    stage4: "test_execution_progress",
};

export class InstanceProgressWidget extends Component {
    static template = "jaeger.InstanceProgressWidget";
    static props = { ...standardWidgetProps };

    get stages() {
        const data = this.props.record.data;
        const currentStage = data.current_stage;
        const currentIdx = STAGE_INDEX[currentStage] ?? -1;
        const isFailed = currentStage === "failed";

        return STAGES.map((stage, idx) => {
            const statusValue = data[stage.field];
            let state;

            if (isFailed && idx === currentIdx) {
                state = "failed";
            } else if (currentIdx > idx || currentStage === "done") {
                state = "done";
            } else if (idx === currentIdx) {
                if (statusValue === "done") {
                    state = "done";
                } else if (["running", "building", "generating", "dispatched", "evaluating", "converting"].includes(statusValue)) {
                    state = "active";
                } else if (statusValue === "failed") {
                    state = "failed";
                } else if (["queued"].includes(statusValue)) {
                    state = "queued";
                } else {
                    state = "current";
                }
            } else {
                state = "idle";
            }

            if (stage.key === "stage4" && state === "current"
                && data.test_execution_status === "done"
                && data.instances_valid_count === 0) {
                state = "warning";
            }

            const progressField = PROGRESS_FIELDS[stage.key];
            const progress = progressField ? Math.round(data[progressField] || 0) : 0;

            return {
                key: stage.key,
                label: stage.label,
                state,
                number: idx + 1,
                progress,
            };
        });
    }

    get connectors() {
        const s = this.stages;
        const result = [];
        for (let i = 0; i < s.length - 1; i++) {
            const from = s[i];
            const to = s[i + 1];
            const filled = from.state === "done";
            result.push({ key: `${from.key}-${to.key}`, filled });
        }
        return result;
    }

    stepClass(step) {
        return `o_jaeger_progress_step o_jaeger_progress_step_${step.state}`;
    }

    connectorClass(conn) {
        return `o_jaeger_progress_connector${conn.filled ? " o_jaeger_progress_connector_filled" : ""}`;
    }

    ringStyle(step) {
        // SVG circle circumference = 2 * PI * r = 2 * PI * 18 ~= 113.1
        const circumference = 113.1;
        const offset = circumference - (circumference * step.progress / 100);
        return `stroke-dashoffset: ${offset}`;
    }
}

export const instanceProgressWidgetDef = { component: InstanceProgressWidget };
registry.category("view_widgets").add("jaeger_instance_progress", instanceProgressWidgetDef);
