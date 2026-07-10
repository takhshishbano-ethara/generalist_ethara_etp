/** @odoo-module */
import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

export class KenseiTrackerDashboard extends Component {
    static template = "kensei.TrackerDashboard";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.notification = useService("notification");

        this.state = useState({
            loading: true,
            teamComposition: [],
            funnel: [],
            latestActivity: [],
            perPl: [],
        });

        onWillStart(() => this._loadData());
    }

    async _loadData() {
        this.state.loading = true;
        try {
            const res = await rpc("/kensei/tracker/dashboard", {});
            if (res && res.error) {
                this.notification.add("You are not allowed to view this dashboard.", {
                    type: "warning",
                });
                return;
            }
            this.state.teamComposition = res.team_composition || [];
            this.state.funnel = res.funnel || [];
            this.state.latestActivity = res.latest_activity || [];
            this.state.perPl = res.per_pl || [];
        } catch (e) {
            this.notification.add("Failed to load dashboard data.", { type: "danger" });
            console.error("[kensei-tracker]", e);
        } finally {
            this.state.loading = false;
        }
    }

    onRefresh() {
        this._loadData();
    }

    /** Display "—" for null/undefined metric values (e.g. avg score). */
    fmt(value) {
        if (value === null || value === undefined || value === "") {
            return "—";
        }
        return typeof value === "number" ? value.toLocaleString("en-US") : value;
    }

    get todayLabel() {
        const now = new Date();
        return now.toLocaleDateString("en-US", {
            year: "numeric",
            month: "long",
            day: "numeric",
        });
    }
}

registry.category("actions").add("kensei_tracker_dashboard", KenseiTrackerDashboard);
