/** @odoo-module */
import { useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { Kensei2DashboardBase } from "@kensei2/dashboard_base/dashboard_base";

/**
 * Per-tasker performance dashboard. Serves two entry points:
 *  - Dashboard menu (plain taskers)  -> no member_id  -> the current user.
 *  - Team Management button          -> params.member_id -> that member
 *    (QL/PL only, enforced server-side).
 */
export class Kensei2TaskerDashboard extends Kensei2DashboardBase {
    static template = "kensei2.TaskerDashboard";

    setup() {
        super.setup();
        this.memberId = this.props.action?.params?.member_id || false;
        this.state = useState({
            loading: true,
            denied: false,
            subject: {},
            kpis: [],
            funnel: [],
            recent: [],
            dateFrom: "",
            dateTo: "",
            lastUpdated: "",
        });
        onWillStart(() => this._load());
    }

    async _load() {
        const res = await this._fetch(
            "/kensei2/tracker/performance",
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
        this.state.recent = res.recent || [];
    }

    get title() {
        return this.memberId ? this.state.subject.name || "Tasker" : "My Dashboard";
    }

    openTask(row) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "kensei2.tracker.allocation",
            res_id: row.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    /** Keyboard equivalent of clicking a recent-task row (the <tr> is t-on-click). */
    onRowKeydown(ev, row) {
        if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            ev.preventDefault();
            this.openTask(row);
        }
    }
}

registry.category("actions").add("kensei2_tasker_dashboard", Kensei2TaskerDashboard);
