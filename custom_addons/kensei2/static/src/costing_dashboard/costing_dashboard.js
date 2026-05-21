/** @odoo-module */
import { Component, useState, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

const PERIODS = [
    { key: "week", label: "This Week" },
    { key: "month", label: "This Month" },
    { key: "all", label: "All Time" },
];

function formatNumber(n) {
    if (!n) return "0";
    return n.toLocaleString("en-US");
}

export class CostingDashboard extends Component {
    static template = "kensei2.CostingDashboard";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.notification = useService("notification");
        this.periods = PERIODS;
        this.formatNumber = formatNumber;

        this.state = useState({
            period: "week",
            loading: false,
            rows: [],
            totals: null,
            startDate: "",
            endDate: "",
            sortField: "grand_total",
            sortAsc: false,
            expanded: {},
            expandedTasks: {},
        });

        onMounted(() => this._loadData());
    }

    async _loadData() {
        this.state.loading = true;
        try {
            const res = await rpc("/kensei2/costing/data", { period: this.state.period });
            this.state.rows = res.rows || [];
            this.state.totals = res.totals || {};
            this.state.startDate = res.start_date || "";
            this.state.endDate = res.end_date || "";
        } catch (e) {
            this.notification.add("Failed to load costing data", { type: "danger" });
            console.error("[kensei2-costing]", e);
        } finally {
            this.state.loading = false;
        }
    }

    onPeriodChange(key) {
        this.state.period = key;
        this._loadData();
    }

    onSort(field) {
        if (this.state.sortField === field) {
            this.state.sortAsc = !this.state.sortAsc;
        } else {
            this.state.sortField = field;
            this.state.sortAsc = false;
        }
    }

    onToggleEmployee(employeeId) {
        this.state.expanded = {
            ...this.state.expanded,
            [employeeId]: !this.state.expanded[employeeId],
        };
    }

    isExpanded(employeeId) {
        return !!this.state.expanded[employeeId];
    }

    onToggleTask(taskId) {
        this.state.expandedTasks = {
            ...this.state.expandedTasks,
            [taskId]: !this.state.expandedTasks[taskId],
        };
    }

    isTaskExpanded(taskId) {
        return !!this.state.expandedTasks[taskId];
    }

    getTrajectoryBarWidth(tokensTotal, task) {
        if (!task.trajectories || task.trajectories.length === 0) return 0;
        const maxTokens = Math.max(...task.trajectories.map(t => t.tokens_total));
        if (maxTokens === 0) return 0;
        return Math.round((tokensTotal / maxTokens) * 100);
    }

    getModelColor(model) {
        const colors = {
            claude: "#d97706",
            glm: "#7c3aed",
            gpt: "#059669",
        };
        return colors[model] || "#6b7280";
    }

    get sortedRows() {
        const rows = [...this.state.rows];
        const f = this.state.sortField;
        const dir = this.state.sortAsc ? 1 : -1;
        rows.sort((a, b) => {
            const va = f === "employee_name" ? (a[f] || "").toLowerCase() : (a[f] || 0);
            const vb = f === "employee_name" ? (b[f] || "").toLowerCase() : (b[f] || 0);
            if (va < vb) return -1 * dir;
            if (va > vb) return 1 * dir;
            return 0;
        });
        return rows;
    }

    get dateRangeLabel() {
        if (!this.state.startDate) return "All Time";
        return `${this.state.startDate}  →  ${this.state.endDate}`;
    }

    sortIcon(field) {
        if (this.state.sortField !== field) return "fa-sort";
        return this.state.sortAsc ? "fa-sort-asc" : "fa-sort-desc";
    }
}

registry.category("actions").add("kensei2_costing_dashboard", CostingDashboard);
