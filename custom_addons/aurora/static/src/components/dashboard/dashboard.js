/** @odoo-module */

import { registry } from "@web/core/registry";
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const REFRESH_INTERVAL = 30000;

export class AuroraDashboard extends Component {
    static template = "aurora.Dashboard";
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.state = useState({
            loaded: false,
            pipelines: { total: 0, running: 0, done: 0, failed: 0, draft: 0 },
            evaluations: { total: 0, running: 0, done: 0, failed: 0, draft: 0 },
            phase1: { total: 0, running: 0, done: 0, failed: 0, draft: 0 },
            phase2: { total: 0, running: 0, done: 0, failed: 0, draft: 0 },
            phase3: { total: 0, running: 0, done: 0, failed: 0, draft: 0 },
            evaluation_results: { total: 0, resolved: 0, unresolved: 0, error: 0, running: 0 },
            harness_staging: { total: 0, deployed: 0, notified: 0, testing: 0, failed: 0 },
            recent_pipelines: [],
            recent_evaluations: [],
            tokens: { active: 0, total: 0 },
        });
        this._timer = null;

        onMounted(async () => {
            await this._loadData();
            this._timer = setInterval(() => this._loadData(), REFRESH_INTERVAL);
        });

        onWillUnmount(() => {
            if (this._timer) clearInterval(this._timer);
        });
    }

    async _loadData() {
        try {
            const data = await this.orm.call("aurora.pipeline", "get_dashboard_data", []);
            Object.assign(this.state, data, { loaded: true });
        } catch {
            this.state.loaded = true;
        }
    }

    openPipelines(stage) {
        const domain = stage ? [["stage", "=", stage]] : [];
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Pipeline Runs",
            res_model: "aurora.pipeline",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain,
        });
    }

    openEvaluations(stage) {
        const domain = stage ? [["stage", "=", stage]] : [];
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Evaluation Runs",
            res_model: "aurora.evaluation",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain,
        });
    }

    openRunningPipelines() { this.openPipelines(false); }
    openRunningEvaluations() { this.openEvaluations(false); }

    openPipelineRecord(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "aurora.pipeline",
            res_id: id,
            view_mode: "form",
            views: [[false, "form"]],
        });
    }

    openEvaluationRecord(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "aurora.evaluation",
            res_id: id,
            view_mode: "form",
            views: [[false, "form"]],
        });
    }

    openNewPipeline() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "aurora.pipeline",
            view_mode: "form",
            views: [[false, "form"]],
        });
    }

    openNewEvaluation() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "aurora.evaluation",
            view_mode: "form",
            views: [[false, "form"]],
        });
    }

    openTokenPool() {
        this.action.doAction("aurora.action_aurora_github_token");
    }

    openEvaluationResults() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Evaluation Results",
            res_model: "aurora.evaluation.instance",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            context: { create: false, delete: false },
        });
    }

    openHarnessStaging() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Harness Staging",
            res_model: "aurora.harness.staging",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
        });
    }

    stageClass(stage) {
        if (stage === "done") return "aurora-dash-tag-success";
        if (stage === "failed") return "aurora-dash-tag-danger";
        if (stage === "draft") return "aurora-dash-tag-muted";
        return "aurora-dash-tag-info";
    }
}

registry.category("actions").add("aurora.dashboard", AuroraDashboard);
