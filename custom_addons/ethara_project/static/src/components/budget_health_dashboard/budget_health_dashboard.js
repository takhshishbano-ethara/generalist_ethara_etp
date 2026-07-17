/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class BudgetHealthDashboard extends Component {
    static template = "ethara_project.BudgetHealthDashboard";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loaded: false,
            totals: {
                budget_amount: 0,
                consumed_amount: 0,
                remaining_amount: 0,
                consumed_pct: 0,
                budget_count: 0,
            },
            health_counts: {
                unknown: 0,
                healthy: 0,
                warning: 0,
                at_risk: 0,
                critical: 0,
            },
            budgets: [],
            searchTerm: "",
        });

        onWillStart(async () => {
            await this.loadData();
        });
    }

    async loadData() {
        const data = await this.orm.call(
            "ethara.project.budget.dashboard",
            "get_dashboard_data",
            [null],
        );
        Object.assign(this.state.totals, data.totals);
        Object.assign(this.state.health_counts, data.health_counts);
        this.state.budgets = data.budgets;
        this.state.loaded = true;
    }

    async refresh() {
        this.state.loaded = false;
        await this.loadData();
    }

    formatUSD(amount) {
        if (amount === undefined || amount === null) return "USD 0.00";
        const abs = Math.abs(amount);
        const sign = amount < 0 ? "-" : "";
        return `${sign}USD ${abs.toLocaleString("en-US", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        })}`;
    }

    formatPct(pct) {
        if (pct === undefined || pct === null) return "0.0%";
        return `${pct.toFixed(1)}%`;
    }

    healthLabel(status) {
        return {
            healthy: "Healthy",
            warning: "Warning",
            at_risk: "At Risk",
            critical: "Critical",
            unknown: "Unknown",
        }[status] || "Unknown";
    }

    healthClass(status) {
        return `ethara-badge ethara-badge--${status || "unknown"}`;
    }

    get filteredBudgets() {
        const q = (this.state.searchTerm || "").trim().toLowerCase();
        if (!q) return this.state.budgets;
        return this.state.budgets.filter(
            (b) =>
                (b.name || "").toLowerCase().includes(q) ||
                (b.project_name || "").toLowerCase().includes(q),
        );
    }

    openBudget(budgetId) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ethara.project.budget",
            res_id: budgetId,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add(
    "ethara_project.budget_health_dashboard",
    BudgetHealthDashboard,
);
