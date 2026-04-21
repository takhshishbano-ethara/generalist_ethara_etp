/** @odoo-module */

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

export class RunDashboardWidget extends Component {
    static template = "jaeger.RunDashboardWidget";
    static props = { ...standardWidgetProps };

    get stats() {
        const data = this.props.record.data;
        const totalRuns = (data.run_ids && data.run_ids.records) ? data.run_ids.records.length : 0;

        return {
            passAtK: data.pass_at_k ? (data.pass_at_k * 100).toFixed(1) : "N/A",
            kRuns: data.k_runs || 8,
            totalCost: data.total_api_cost ? `$${data.total_api_cost.toFixed(2)}` : "$0.00",
            totalCalls: data.total_api_calls || 0,
            promptTokens: this._formatNumber(data.total_prompt_tokens || 0),
            completionTokens: this._formatNumber(data.total_completion_tokens || 0),
            trajectoryStatus: data.trajectory_status || "pending",
            eksJobId: data.eks_job_id || "",
            modelName: data.model_name || "claude",
        };
    }

    get statusClass() {
        const status = this.props.record.data.trajectory_status;
        const classMap = {
            pending: "text-muted",
            dispatched: "text-info",
            running: "text-primary",
            evaluating: "text-warning",
            summarizing: "text-warning",
            done: "text-success",
            failed: "text-danger",
        };
        return classMap[status] || "text-muted";
    }

    _formatNumber(n) {
        if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
        if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
        return String(n);
    }
}

export const runDashboardWidgetDef = { component: RunDashboardWidget };
registry.category("view_widgets").add("jaeger_run_dashboard", runDashboardWidgetDef);
