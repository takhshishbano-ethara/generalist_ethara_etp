/** @odoo-module */
import { useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { KenseiDashboardBase } from "@kensei/dashboard_base/dashboard_base";

/**
 * Per-tasker performance dashboard. Serves two entry points:
 *  - Dashboard menu (plain taskers)  -> no member_id  -> the current user.
 *  - Team Management button          -> params.member_id -> that member
 *    (QL/PL only, enforced server-side).
 */
export class KenseiTaskerDashboard extends KenseiDashboardBase {
    static template = "kensei.TaskerDashboard";

    setup() {
        super.setup();
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
        const res = await this._fetch(
            "/kensei/tracker/performance",
            { member_id: this.memberId || false },
            "Failed to load performance data.");
        if (!res) {
            return;
        }
        if (res.error) {
            this.state.denied = true;
            this.notification.add(
                res.error === "access_denied"
                    ? "You are not allowed to view this tasker."
                    : "No performance data available.",
                { type: "warning" });
            return;
        }
        this.state.denied = false;
        this.state.subject = res.subject || {};
        this.state.kpis = res.kpis || [];
        this.state.funnel = res.funnel || [];
        this.state.trend = res.trend || [];
        this.state.recent = res.recent || [];
    }

    get title() {
        return this.memberId ? this.state.subject.name || "Tasker" : "My Dashboard";
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
