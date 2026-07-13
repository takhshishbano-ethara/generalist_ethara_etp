/** @odoo-module */
import { Component, useState } from "@odoo/owl";

/**
 * Reusable per-<group> progress table (Per-PL / Per-QL / Per-Tasker) with
 * built-in client-side pagination. One place = consistent columns + paging
 * across every dashboard table.
 */
export class ProgressTable extends Component {
    static template = "kensei.ProgressTable";
    static props = {
        rows: { type: Array },
        head: { type: String },
        onRowClick: { type: Function, optional: true },
        pageSize: { type: Number, optional: true },
    };

    setup() {
        this.state = useState({ page: 1 });
    }

    get size() {
        return this.props.pageSize || 10;
    }
    get total() {
        return this.props.rows.length;
    }
    get totalPages() {
        return Math.max(1, Math.ceil(this.total / this.size));
    }
    get pageRows() {
        // clamp the page in case rows shrank (e.g. after a date-filter change)
        const page = Math.min(this.state.page, this.totalPages);
        const start = (page - 1) * this.size;
        return this.props.rows
            .slice(start, start + this.size)
            .map((r, i) => ({ ...r, _idx: start + i + 1 }));
    }

    prev() {
        if (this.state.page > 1) {
            this.state.page--;
        }
    }
    next() {
        if (this.state.page < this.totalPages) {
            this.state.page++;
        }
    }

    rowClick(row) {
        if (this.props.onRowClick) {
            this.props.onRowClick(row);
        }
    }

    fmt(v) {
        return v === null || v === undefined || v === "" ? "—" : v;
    }
}
