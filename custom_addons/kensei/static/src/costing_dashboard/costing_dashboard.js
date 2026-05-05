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
    static template = "kensei.CostingDashboard";
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
        });

        onMounted(() => this._loadData());
    }

    async _loadData() {
        this.state.loading = true;
        try {
            const res = await rpc("/kensei/costing/data", { period: this.state.period });
            this.state.rows = res.rows || [];
            this.state.totals = res.totals || {};
            this.state.startDate = res.start_date || "";
            this.state.endDate = res.end_date || "";
        } catch (e) {
            this.notification.add("Failed to load costing data", { type: "danger" });
            console.error("[kensei-costing]", e);
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

registry.category("actions").add("kensei_costing_dashboard", CostingDashboard);
