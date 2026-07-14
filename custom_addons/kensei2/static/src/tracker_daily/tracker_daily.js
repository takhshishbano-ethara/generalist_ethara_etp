/** @odoo-module */
import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { todayIso, daysAgoIso } from "@kensei2/tracker_common";

export class Kensei2TrackerDaily extends Component {
    static template = "kensei2.TrackerDaily";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.notification = useService("notification");
        // Monotonic request counter — see _load(). Guards against a slow early
        // response overwriting a fresh one when filters change quickly.
        this._seq = 0;

        this.state = useState({
            loading: true,
            dates: [],
            rows: [],
            summary: {},
            totalRows: 0,
            page: 1,
            pageSize: 20,
            sortBy: "name",
            sortDir: "asc",
            // filter options
            opts: { pls: [], team_leads: [], projects: [], statuses: [] },
            // filter values
            filters: {
                date_from: daysAgoIso(6),
                date_to: todayIso(),
                employee: "",
                pl_id: "",
                status: "",
                project: "",
                team_lead_id: "",
            },
        });

        onWillStart(async () => {
            await this._loadFilters();
            await this._load();
        });
    }

    async _loadFilters() {
        try {
            this.state.opts = await rpc("/kensei2/tracker/daily/filters", {});
        } catch (e) {
            console.error("[kensei2-daily] filters", e);
        }
    }

    _params(extra = {}) {
        const f = this.state.filters;
        return {
            date_from: f.date_from,
            date_to: f.date_to,
            employee: f.employee,
            pl_id: f.pl_id,
            status: f.status,
            project: f.project,
            team_lead_id: f.team_lead_id,
            sort_by: this.state.sortBy,
            sort_dir: this.state.sortDir,
            page: this.state.page,
            page_size: this.state.pageSize,
            ...extra,
        };
    }

    /**
     * Load the pivot, dropping any response that a newer request has superseded.
     *
     * Every control here — Apply, sort, paginate — calls _load() straight away, so
     * two quick clicks put two requests in flight. Without the sequence check the
     * SLOWER one resolves last and writes ITS rows into state: the table then shows
     * results for a filter the user already moved off, and the pager disagrees with
     * the rows. Kensei2DashboardBase._fetch guards this for the other dashboards;
     * this component does not extend it (see the note on the class), so it carries
     * its own guard rather than silently going without one.
     */
    async _load() {
        const seq = ++this._seq;
        const isStale = () => seq !== this._seq;
        this.state.loading = true;
        try {
            const res = await rpc("/kensei2/tracker/daily/data", this._params());
            if (isStale()) {
                return;
            }
            this.state.dates = res.dates || [];
            this.state.rows = res.rows || [];
            this.state.summary = res.summary || {};
            this.state.totalRows = res.total_rows || 0;
            this.state.page = res.page || 1;
        } catch (e) {
            if (isStale()) {
                return;
            }
            this.notification.add("Failed to load Daily Tracker data.", { type: "danger" });
            console.error("[kensei2-daily]", e);
        } finally {
            // only the newest request owns the spinner
            if (!isStale()) {
                this.state.loading = false;
            }
        }
    }

    onFilterInput(field, ev) {
        this.state.filters[field] = ev.target.value;
    }

    onApply() {
        this.state.page = 1;
        this._load();
    }

    onReset() {
        this.state.filters = {
            date_from: daysAgoIso(6),
            date_to: todayIso(),
            employee: "",
            pl_id: "",
            status: "",
            project: "",
            team_lead_id: "",
        };
        this.state.page = 1;
        this._load();
    }

    onSort(field) {
        if (this.state.sortBy === field) {
            this.state.sortDir = this.state.sortDir === "asc" ? "desc" : "asc";
        } else {
            this.state.sortBy = field;
            this.state.sortDir = "asc";
        }
        this._load();
    }

    sortIcon(field) {
        if (this.state.sortBy !== field) return "fa-sort";
        return this.state.sortDir === "asc" ? "fa-sort-asc" : "fa-sort-desc";
    }

    get totalPages() {
        return Math.max(1, Math.ceil(this.state.totalRows / this.state.pageSize));
    }

    onPrevPage() {
        if (this.state.page > 1) {
            this.state.page--;
            this._load();
        }
    }
    onNextPage() {
        if (this.state.page < this.totalPages) {
            this.state.page++;
            this._load();
        }
    }

    cellClass(row, col) {
        const v = row.cells[col.key] || 0;
        // o_kd_*, not o_ktd_*. This file's stylesheet (tracker_daily.scss) defines
        // .o_kd_cell / .o_kd_weekend / .o_kd_has; o_ktd_* belongs to the OTHER
        // dashboard (tracker_dashboard.scss). With the wrong prefix these three
        // classes matched nothing, so weekend shading and the green "has
        // completions" highlight never rendered in the table body — while the <th>,
        // which correctly used o_kd_weekend, did shade. Hence shaded headers above
        // unshaded columns.
        let cls = "o_kd_cell";
        if (col.weekend) cls += " o_kd_weekend";
        if (v > 0) cls += " o_kd_has";
        return cls;
    }

    async onExport() {
        try {
            const res = await rpc("/kensei2/tracker/daily/data", this._params({ export: true, page_size: 100000 }));
            const dates = res.dates || [];
            const rows = res.rows || [];
            const header = ["Employee Name", "PL", ...dates.map((d) => d.label), "Total"];
            const lines = [header.map(csvCell).join(",")];
            for (const r of rows) {
                const line = [r.name, r.pl, ...dates.map((d) => r.cells[d.key] || 0), r.total];
                lines.push(line.map(csvCell).join(","));
            }
            const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8;" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = "daily_tracker.csv";
            a.click();
            URL.revokeObjectURL(url);
        } catch (e) {
            this.notification.add("Export failed.", { type: "danger" });
            console.error("[kensei2-daily] export", e);
        }
    }
}

function csvCell(v) {
    const s = String(v == null ? "" : v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

registry.category("actions").add("kensei2_tracker_daily", Kensei2TrackerDaily);
