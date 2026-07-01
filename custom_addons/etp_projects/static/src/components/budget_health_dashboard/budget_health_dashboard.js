/** @odoo-module */

import { registry } from "@web/core/registry";
import { Component, useState, onMounted } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class BudgetHealthDashboard extends Component {
    static template = "etp_projects.BudgetHealthDashboard";
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.state = useState({
            loaded: false,
            error: null,
            data: null,
            filters: {
                project_ids: [],
                project_classification: "",
                budget_type: "",
                start_month: "",
                end_month: "",
            },
            projectFilterOpen: false,
        });
        onMounted(() => this._loadData());
    }

    async _loadData() {
        this.state.error = null;
        try {
            const data = await this.orm.call(
                "etp.project.budget.dashboard",
                "get_dashboard_data",
                [this.state.filters],
            );
            this.state.data = data;
            this.state.loaded = true;
        } catch (e) {
            this.state.error = (e && e.message) || "Failed to load dashboard data.";
            this.state.loaded = true;
        }
    }

    async refresh() {
        await this.orm.call("etp.project.budget.dashboard", "action_refresh_all", []);
        await this._loadData();
    }

    setBudgetType(value) {
        this.state.filters.budget_type = value;
        this._loadData();
    }

    setClassification(value) {
        this.state.filters.project_classification = value;
        this._loadData();
    }

    setStartMonth(ev) {
        this.state.filters.start_month = ev.target.value;
        this._loadData();
    }

    setEndMonth(ev) {
        this.state.filters.end_month = ev.target.value;
        this._loadData();
    }

    toggleProject(id) {
        const list = this.state.filters.project_ids;
        const i = list.indexOf(id);
        if (i >= 0) {
            list.splice(i, 1);
        } else {
            list.push(id);
        }
        this._loadData();
    }

    toggleProjectFilter() {
        this.state.projectFilterOpen = !this.state.projectFilterOpen;
    }

    clearFilters() {
        this.state.filters = {
            project_ids: [],
            project_classification: "",
            budget_type: "",
            start_month: "",
            end_month: "",
        };
        this._loadData();
    }

    openRowsList() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Budget Rollups (Raw)",
            res_model: "etp.project.budget.dashboard",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
        });
    }

    openProject(projectId) {
        if (!projectId) return;
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "project.project",
            res_id: projectId,
            view_mode: "form",
            views: [[false, "form"]],
        });
    }

    fmtMoney(v) {
        const sym = (this.state.data && this.state.data.currency_symbol) || "$";
        const n = Number(v) || 0;
        const formatted = n.toLocaleString("en-US", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
        return sym + formatted;
    }

    fmtPct(v) {
        return (Number(v) || 0).toFixed(2) + "%";
    }

    healthClass(h) {
        const map = {
            healthy: "etp-bdh-healthy",
            warning: "etp-bdh-warning",
            at_risk: "etp-bdh-at-risk",
            critical: "etp-bdh-critical",
            unknown: "etp-bdh-unknown",
        };
        return map[h] || "etp-bdh-unknown";
    }

    healthLabel(h) {
        const map = {
            healthy: "Healthy",
            warning: "Warning",
            at_risk: "At Risk",
            critical: "Critical",
            unknown: "Unknown",
        };
        return map[h] || h;
    }

    barWidth(value, max) {
        if (!max) return "0%";
        const ratio = Math.abs(Number(value) || 0) / Math.abs(Number(max) || 1);
        return Math.min(100, ratio * 100) + "%";
    }

    maxOf(rows, key) {
        if (!rows || !rows.length) return 0;
        return Math.max(...rows.map((r) => Math.abs(Number(r[key]) || 0)));
    }

    isProjectSelected(id) {
        return this.state.filters.project_ids.indexOf(id) >= 0;
    }

    get selectedProjectsLabel() {
        const ids = this.state.filters.project_ids;
        if (!ids.length) return "All Projects";
        if (ids.length === 1) {
            const p = (this.state.data && this.state.data.filter_options.projects || [])
                .find((x) => x.id === ids[0]);
            return p ? p.name : "1 project";
        }
        return ids.length + " projects";
    }
}

registry.category("actions").add("etp_project_budget_health_dashboard", BudgetHealthDashboard);
