/** @odoo-module */
import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { isoLocal, fmt } from "@kensei/tracker_common";

/**
 * Per-tasker performance dashboard. Serves two entry points:
 *  - "My Performance" menu  -> no member_id  -> the current user.
 *  - Team Management button  -> params.member_id -> that member (QL/PL only,
 *    enforced server-side).
 */
export class KenseiTaskerDashboard extends Component {
    static template = "kensei.TaskerDashboard";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.notification = useService("notification");
        this.action = useService("action");
        this.fmt = fmt;  // exposed for the template
        this.memberId = this.props.action?.params?.member_id || false;

        this.state = useState({
            loading: true,
            denied: false,
            subject: {},
            kpis: [],
            funnel: [],
            trend: [],
            recent: [],
            dateFrom: "",
            dateTo: "",
            lastUpdated: "",
        });

        onWillStart(() => this._load());
    }

    async _load() {
        this.state.loading = true;
        try {
            const res = await rpc("/kensei/tracker/performance", {
                member_id: this.memberId || false,
                date_from: this.state.dateFrom || false,
                date_to: this.state.dateTo || false,
            });
            if (res && res.error) {
                this.state.denied = true;
                this.notification.add(
                    res.error === "access_denied"
                        ? "You are not allowed to view this tasker."
                        : "No performance data available.",
                    { type: "warning" }
                );
                return;
            }
            this.state.denied = false;
            this.state.subject = res.subject || {};
            this.state.kpis = res.kpis || [];
            this.state.funnel = res.funnel || [];
            this.state.trend = res.trend || [];
            this.state.recent = res.recent || [];
            this.state.lastUpdated = new Date().toLocaleTimeString("en-US", {
                hour: "2-digit",
                minute: "2-digit",
            });
        } catch (e) {
            this.notification.add("Failed to load performance data.", { type: "danger" });
            console.error("[kensei-tasker]", e);
        } finally {
            this.state.loading = false;
        }
    }

    onRefresh() {
        this._load();
    }

    // ---- date range ----
    setPreset(days) {
        if (days === null) {
            this.state.dateFrom = "";
            this.state.dateTo = "";
        } else {
            const to = new Date();
            const from = new Date();
            from.setDate(to.getDate() - (days - 1));
            this.state.dateFrom = isoLocal(from);
            this.state.dateTo = isoLocal(to);
        }
        this._load();
    }
    onDateInput(which, ev) {
        this.state[which] = ev.target.value || "";
        this._load();
    }
    get isFiltered() {
        return Boolean(this.state.dateFrom || this.state.dateTo);
    }

    get title() {
        return this.memberId ? this.state.subject.name || "Tasker" : "My Performance";
    }

    get maxTrend() {
        return Math.max(1, ...this.state.trend.map((t) => t.value));
    }

    trendHeight(value) {
        return Math.round((value / this.maxTrend) * 100);
    }

    openTask(row) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "kensei.tracker.allocation",
            res_id: row.id,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("kensei_tasker_dashboard", KenseiTaskerDashboard);
