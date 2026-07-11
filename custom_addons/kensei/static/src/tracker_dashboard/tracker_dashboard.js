/** @odoo-module */
import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { isoLocal, fmt } from "@kensei/tracker_common";

export class KenseiTrackerDashboard extends Component {
    static template = "kensei.TrackerDashboard";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.notification = useService("notification");
        this.action = useService("action");
        this.fmt = fmt;  // exposed for the template

        this.state = useState({
            loading: true,
            teamComposition: [],
            funnel: [],
            latestActivity: [],
            perPl: [],
            perQl: [],
            lastUpdated: "",
            dateFrom: "",
            dateTo: "",
        });

        onWillStart(() => this._loadData());
    }

    async _loadData() {
        this.state.loading = true;
        try {
            const res = await rpc("/kensei/tracker/dashboard", {
                date_from: this.state.dateFrom || false,
                date_to: this.state.dateTo || false,
            });
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
            this.state.perQl = res.per_ql || [];
            this.state.lastUpdated = new Date().toLocaleTimeString("en-US", {
                hour: "2-digit",
                minute: "2-digit",
            });
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

    // ---- date-range filter (scopes allocation sections by assignment date) ----
    /** days=null clears the range (All time); otherwise the last N days incl. today. */
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
        this._loadData();
    }

    onDateInput(which, ev) {
        this.state[which] = ev.target.value || "";
        this._loadData();
    }

    get isFiltered() {
        return Boolean(this.state.dateFrom || this.state.dateTo);
    }

    // ---- drill-down: open the filtered Task Allocation / Team Management list ----
    _openAllocations(domain, name) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: "kensei.tracker.allocation",
            domain,
            views: [
                [false, "list"],
                [false, "form"],
            ],
            target: "current",
        });
    }

    _openMembers(domain, name) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: "kensei.tracker.team.member",
            domain,
            views: [
                [false, "list"],
                [false, "form"],
            ],
            target: "current",
        });
    }

    onCompositionClick(card) {
        const domain = card.role ? [["role", "=", card.role]] : [];
        this._openMembers(domain, `Team — ${card.label}`);
    }

    onStatusCardClick(card) {
        if (!card.statuses || !card.statuses.length) {
            return; // e.g. "Avg Score" has nothing to open
        }
        this._openAllocations([["status", "in", card.statuses]], card.label);
    }

    onPlClick(row) {
        const domain = row.id ? [["pl_id", "=", row.id]] : [["pl_id", "=", false]];
        this._openAllocations(domain, `PL — ${row.name}`);
    }

    onQlClick(row) {
        const domain = row.id
            ? [["assigned_ql_id", "=", row.id]]
            : [["assigned_ql_id", "=", false]];
        this._openAllocations(domain, `QL — ${row.name}`);
    }
}

registry.category("actions").add("kensei_tracker_dashboard", KenseiTrackerDashboard);
