/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState, onWillUnmount } from "@odoo/owl";

// T2AV Dashboard — a landing dashboard (not a list):
// live KPI cards, lifecycle + category breakdowns, recent feed,
// refresh + auto-refresh, time-window filters, and click-to-drill-down
// into the underlying t2av.generation records.

const AUTO_REFRESH_MS = 15000;

export class T2AVDashboard extends Component {
    static template = "t2av.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            window: "all",        // all | 24h | 7d
            data: null,
        });

        onWillStart(async () => {
            await this.load();
            this._timer = setInterval(() => this.load(), AUTO_REFRESH_MS);
        });
        onWillUnmount(() => this._timer && clearInterval(this._timer));
    }

    // domain derived from the active time-window filter
    get domain() {
        if (this.state.window === "24h") {
            return [["create_date", ">=", this._daysAgo(1)]];
        }
        if (this.state.window === "7d") {
            return [["create_date", ">=", this._daysAgo(7)]];
        }
        return [];
    }

    _daysAgo(n) {
        const d = new Date();
        d.setDate(d.getDate() - n);
        return d.toISOString().slice(0, 19).replace("T", " ");
    }

    async load() {
        const data = await this.orm.call(
            "t2av.dashboard", "get_dashboard_data", [this.domain],
        );
        this.state.data = data;
        this.state.loading = false;
    }

    setWindow(w) {
        this.state.window = w;
        this.load();
    }

    // ---- drill-downs: open the native Generations list with ONLY our
    //      filter applied (fresh action, empty context = no default filter,
    //      no search panel). The dashboard IS the navigation. ---------------
    openList(title, extraDomain) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: title,
            res_model: "t2av.generation",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain: this.domain.concat(extraDomain || []),
            target: "current",
            context: {},
        });
    }

    openRecord(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "t2av.generation",
            res_id: id,
            view_mode: "form",
            views: [[false, "form"]],
            target: "current",
        });
    }

    openByState(state) { this.openList("Status: " + state, [["state", "=", state]]); }
    openByCategory(slug, label) { this.openList(label, [["category", "=", slug]]); }
    openInProgress() {
        this.openList("In Progress",
            [["state", "in", ["queued", "submitting", "processing", "downloading"]]]);
    }
    openFailed() {
        this.openList("Failed / Cancelled",
            [["state", "in", ["failed", "cancelled"]]]);
    }
    openImport() {
        this.action.doAction("t2av.action_t2av_import_wizard");
    }

    // ---- Bootstrap badge class per lifecycle state -----------------------
    stateBadge(state) {
        if (state === "done") return "text-bg-success";
        if (state === "failed" || state === "cancelled") return "text-bg-danger";
        if (state === "draft") return "text-bg-light border";
        return "text-bg-info";
    }
}

registry.category("actions").add("t2av_dashboard", T2AVDashboard);
