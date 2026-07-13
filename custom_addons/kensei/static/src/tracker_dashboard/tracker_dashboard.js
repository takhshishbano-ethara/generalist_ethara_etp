/** @odoo-module */
import { useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { KenseiDashboardBase } from "@kensei/dashboard_base/dashboard_base";
import { ProgressTable } from "@kensei/progress_table/progress_table";

export class KenseiTrackerDashboard extends KenseiDashboardBase {
    static template = "kensei.TrackerDashboard";
    static components = { ProgressTable };

    setup() {
        super.setup();
        this.state = useState({
            loading: true,
            teamComposition: [],
            funnel: [],
            stats: [],
            perPl: [],
            perQl: [],
            perTasker: [],
            lastUpdated: "",
            dateFrom: "",
            dateTo: "",
        });
        onWillStart(() => this._load());
    }

    async _load() {
        const res = await this._fetch(
            "/kensei/tracker/dashboard", {}, "Failed to load dashboard data.");
        if (!res) {
            return;
        }
        if (res.error) {
            this.notification.add("You are not allowed to view this dashboard.",
                { type: "warning" });
            return;
        }
        this.state.teamComposition = res.team_composition || [];
        this.state.funnel = res.funnel || [];
        this.state.stats = res.stats || [];
        this.state.perPl = res.per_pl || [];
        this.state.perQl = res.per_ql || [];
        this.state.perTasker = res.per_tasker || [];
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

    onTaskerClick(row) {
        const domain = row.id
            ? [["tasker_member_id", "=", row.id]]
            : [["tasker_member_id", "=", false]];
        this._openAllocations(domain, `Tasker — ${row.name}`);
    }
}

registry.category("actions").add("kensei_tracker_dashboard", KenseiTrackerDashboard);
