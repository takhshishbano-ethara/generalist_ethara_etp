/** @odoo-module */
import { Component, useState } from "@odoo/owl";

/**
 * Reusable per-<group> progress table (Per-PL / Per-QL / Per-Tasker) with
 * built-in client-side pagination. One place = consistent columns + paging
 * across every dashboard table.
 */
export class ProgressTable extends Component {
    static template = "kensei2.ProgressTable";
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
    /**
     * The effective page — always within range.
     *
     * `state.page` is the user's *intent* and can go stale: when a filter shrinks
     * the row set from 5 pages to 3, state.page is still 5. Reading through this
     * getter everywhere (template, pageRows, prev/next) means the stale value can
     * never be observed. Clamping only inside pageRows, as this used to, left the
     * pager rendering "5 / 3" and made prev() burn two clicks stepping 5 → 4 → 3
     * with nothing changing on screen.
     */
    get page() {
        return Math.min(Math.max(1, this.state.page), this.totalPages);
    }
    get pageRows() {
        const start = (this.page - 1) * this.size;
        return this.props.rows
            .slice(start, start + this.size)
            .map((r, i) => ({ ...r, _idx: start + i + 1 }));
    }

    prev() {
        // step from the CLAMPED page, so the first click always moves
        if (this.page > 1) {
            this.state.page = this.page - 1;
        }
    }
    next() {
        if (this.page < this.totalPages) {
            this.state.page = this.page + 1;
        }
    }

    rowClick(row) {
        if (this.props.onRowClick) {
            this.props.onRowClick(row);
        }
    }

    /**
     * Keyboard equivalent of rowClick. The drill-down rows are <tr t-on-click>, so
     * without this the entire drill-down is mouse-only — unreachable by keyboard
     * and invisible to a screen reader.
     */
    rowKeydown(ev, row) {
        if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            ev.preventDefault();
            this.rowClick(row);
        }
    }

    fmt(v) {
        return v === null || v === undefined || v === "" ? "—" : v;
    }
}
